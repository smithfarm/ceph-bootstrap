# pylint: disable=arguments-differ
import itertools
import logging
import fnmatch
import json


from pyparsing import alphanums, OneOrMore, Optional, Regex, Suppress, Word, QuotedString

import configshell_fb as configshell
from configshell_fb.shell import locatedExpr

from .core import CephNodeManager, SshKeyManager, CephNode
from .exceptions import (
    CephSaltException,
    MinionDoesNotExistInConfiguration,
    PillarFileNotPureYaml,
    ParamsException
)
from .params_helper import BooleanStringValidator, BooleanStringTransformer
from .salt_utils import GrainsManager, PillarManager, SaltClient, CephOrch
from .terminal_utils import PrettyPrinter as PP
from .validate.config import validate_config
from .validate.salt_master import check_salt_master_status, CephSaltPillarNotConfigured


logger = logging.getLogger(__name__)


class OptionHandler:
    def value(self):
        return None, None

    def save(self, value):
        pass

    def reset(self):
        pass

    def read_only(self):
        return False

    def possible_values(self):
        return []

    # pylint: disable=unused-argument
    def children_handler(self, child_name):
        return None

    def commands_map(self):
        return {}


class PillarHandler(OptionHandler):
    def __init__(self, pillar_path):
        self.pillar_path = pillar_path

    def value(self):
        return PillarManager.get(self.pillar_path), None

    def save(self, value):
        PillarManager.set(self.pillar_path, value)

    def reset(self):
        PillarManager.reset(self.pillar_path)

    def read_only(self):
        return False


class BootstrapMinionHandler(PillarHandler):
    def __init__(self):
        super().__init__('ceph-salt:bootstrap_minion')

    def save(self, value):
        if value not in self.possible_values():
            raise MinionDoesNotExistInConfiguration(value)
        node = CephNodeManager.ceph_salt_nodes()[value]
        PillarManager.set('ceph-salt:bootstrap_mon_ip', node.public_ip)
        super().save(value)

    def possible_values(self):
        return [n.minion_id for n in CephNodeManager.ceph_salt_nodes().values()]


class RolesGroupHandler(OptionHandler):
    def value(self):
        return '', None


class RoleElementHandler(OptionHandler):
    def __init__(self, ceph_salt_node, role):
        self.ceph_salt_node = ceph_salt_node
        self.role = role

    def value(self):
        roles = CephNodeManager.all_roles(self.ceph_salt_node)
        if not roles - {self.role}:
            return 'No other roles', None
        return "Other roles: {}".format(", ".join(roles - {self.role})), None


class RoleHandler(OptionHandler):
    def __init__(self, role):
        self.role = role
        self._value = set()

    def _load(self):
        self._value = {n.minion_id for n in CephNodeManager.ceph_salt_nodes().values()
                       if self.role in n.roles}

    def possible_values(self):
        self._load()
        return [n.minion_id for n in CephNodeManager.ceph_salt_nodes().values()]

    def value(self):
        self._load()
        return self._value, True

    def save(self, value):
        self._load()
        _minions = set(value)
        to_remove = self._value - _minions
        to_add = _minions - self._value

        for minion in to_remove:
            CephNodeManager.ceph_salt_nodes()[minion].roles.remove(self.role)
            CephNodeManager.ceph_salt_nodes()[minion].save()

        for minion in to_add:
            CephNodeManager.ceph_salt_nodes()[minion].add_role(self.role)
            CephNodeManager.ceph_salt_nodes()[minion].save()

        CephNodeManager.save_in_pillar()

        self._value = set(value)

    def children_handler(self, child_name):
        return RoleElementHandler(CephNodeManager.ceph_salt_nodes()[child_name], self.role)


class CephSaltNodeHandler(OptionHandler):
    def __init__(self, ceph_salt_node):
        self.ceph_salt_node = ceph_salt_node

    def value(self):
        roles = CephNodeManager.all_roles(self.ceph_salt_node)
        if not roles:
            return 'no roles', None
        return ", ".join(roles), None


class CephSaltNodesHandler(OptionHandler):
    def __init__(self):
        self._minions = set()
        self._ceph_salt_nodes = set()

    def value(self):
        self._ceph_salt_nodes = {n.minion_id for n in CephNodeManager.ceph_salt_nodes().values()}
        return self._ceph_salt_nodes, True

    def save(self, value):
        _value = set(value)
        to_remove = self._ceph_salt_nodes - _value
        to_add = _value - self._ceph_salt_nodes

        for minion in to_remove:
            CephNodeManager.remove_node(minion)
        for minion in to_add:
            CephNodeManager.add_node(minion)

        self._ceph_salt_nodes = set(value)

    def possible_values(self):
        if not self._minions:
            self._minions = set(CephNodeManager.list_all_minions())
        return self._minions - self._ceph_salt_nodes

    def children_handler(self, child_name):
        return CephSaltNodeHandler(CephNodeManager.ceph_salt_nodes()[child_name])


class SSHGroupHandler(OptionHandler):
    def commands_map(self):
        return {
            'generate': self.generate_key_pair
        }

    def generate_key_pair(self):
        private_key, public_key = SshKeyManager.generate_key_pair()
        PillarManager.set('ceph-salt:ssh:private_key', private_key)
        PillarManager.set('ceph-salt:ssh:public_key', public_key)
        PP.pl_green('Key pair generated.')

    def value(self):
        stored_priv_key = PillarManager.get('ceph-salt:ssh:private_key')
        stored_pub_key = PillarManager.get('ceph-salt:ssh:public_key')
        if not stored_priv_key and not stored_pub_key:
            return "no key pair set", False
        if not stored_priv_key or not stored_pub_key:
            return "invalid key pair", False
        try:
            SshKeyManager.check_keys(stored_priv_key, stored_pub_key)
            return "Key Pair set", True
        except Exception:  # pylint: disable=broad-except
            return "invalid key pair", False


class SshPrivateKeyHandler(PillarHandler):
    def __init__(self):
        super(SshPrivateKeyHandler, self).__init__('ceph-salt:ssh:private_key')

    def value(self):
        stored_priv_key, _ = super(SshPrivateKeyHandler, self).value()
        stored_pub_key = PillarManager.get('ceph-salt:ssh:public_key')
        try:
            SshKeyManager.check_private_key(stored_priv_key, stored_pub_key)
            return SshKeyManager.key_fingerprint(stored_pub_key), None
        except Exception as ex:  # pylint: disable=broad-except
            return str(ex), False


class SshPublicKeyHandler(PillarHandler):
    def __init__(self):
        super(SshPublicKeyHandler, self).__init__('ceph-salt:ssh:public_key')

    def value(self):
        stored_pub_key, _ = super(SshPublicKeyHandler, self).value()
        stored_priv_key = PillarManager.get('ceph-salt:ssh:private_key')
        try:
            SshKeyManager.check_public_key(stored_priv_key, stored_pub_key)
            return SshKeyManager.key_fingerprint(stored_pub_key), None
        except Exception as ex:  # pylint: disable=broad-except
            return str(ex), False


class TimeServerGroupHandler(OptionHandler):
    def commands_map(self):
        return {
            'enable': self.enable,
            'disable': self.disable
        }

    def enable(self):
        PillarManager.set('ceph-salt:time_server:enabled', True)
        PP.pl_green('Enabled.')

    def disable(self):
        PillarManager.set('ceph-salt:time_server:enabled', False)
        PP.pl_green('Disabled.')

    def value(self):
        val = PillarManager.get('ceph-salt:time_server:enabled')
        if val is None:
            return "enabled", True
        if val:  # enabled
            host = PillarManager.get('ceph-salt:time_server:server_host')
            if host is None:
                return "enabled, no server host set", False

        return ("enabled", True) if val else ("disabled", True)


class TimeServerHandler(PillarHandler):
    def __init__(self):
        super().__init__('ceph-salt:time_server:server_host')

    def save(self, value):
        node = CephNodeManager.ceph_salt_node_by_hostname(value)
        if node:
            PillarManager.set('ceph-salt:time_server:subnet', node.public_subnet)
            PillarManager.set('ceph-salt:time_server:is_minion', True)
        else:
            PillarManager.reset('ceph-salt:time_server:subnet')
            PillarManager.set('ceph-salt:time_server:is_minion', False)
        super().save(value)

    def possible_values(self):
        return [n.minion_id for n in CephNodeManager.ceph_salt_nodes().values()]


class TimeSubnetHandler(PillarHandler):
    def __init__(self):
        super().__init__('ceph-salt:time_server:subnet')

    def possible_values(self):
        time_server_host = PillarManager.get('ceph-salt:time_server:server_host')
        if time_server_host:
            node = CephNodeManager.ceph_salt_node_by_hostname(time_server_host)
            if node and node.subnets:
                return node.subnets
        return []


CEPH_SALT_OPTIONS = {
    'ceph_cluster': {
        'help': '''
                Cluster Options Configuration
                ====================================
                Options to specify the structure of the Ceph cluster, like
                membership, roles, etc...
                ''',
        'options': {
            'minions': {
                'help': 'The list of salt minions that are used to deploy Ceph',
                'default': [],
                'type': 'minions',
                'handler': CephSaltNodesHandler()
            },
            'roles': {
                'type': 'group',
                'handler': RolesGroupHandler(),
                'help': '''
                        Roles Configuration
                        ====================================
                        ''',
                'options': {
                    'admin': {
                        'type': 'minions',
                        'default': [],
                        'handler': RoleHandler('admin'),
                        'help': 'List of minions with Admin role'
                    },
                    'bootstrap': {
                        'help': 'Cluster\'s first Mon and Mgr',
                        'handler': BootstrapMinionHandler(),
                        'required': True,
                        'default_text': 'no minion',
                        'default': None
                    },
                }
            },
        }
    },
    'containers': {
        'help': '''
                Container Options Configuration
                ====================================
                Options to control the configuration of the Ceph containers used
                for deployment.
                ''',
        'options': {
            'images': {
                'type': 'group',
                'help': "Container images paths",
                'options': {
                    'ceph': {
                        'help': 'Full path of Ceph container image',
                        'default_text': 'no image path',
                        'required': True,
                        'handler': PillarHandler('ceph-salt:container:images:ceph')
                    },
                }
            },
            'registries': {
                'type': 'list_dict',
                'default': [],
                'help': '''
                        List of custom registries in v2 format.
                        =======================================

                        Add by specifying B{location}, B{prefix}, and B{insecure}. e.g.,

                          add location=172.17.0.1:5000/docker.io prefix=docker.io insecure=true
                        ''',
                'params_spec': {
                    'location': {
                        'required': True
                    },
                    'prefix': {},
                    'insecure': {
                        'validator': BooleanStringValidator,
                        'transformer': BooleanStringTransformer
                    },
                    'blocked': {
                        'validator': BooleanStringValidator,
                        'transformer': BooleanStringTransformer
                    }
                },
                'handler': PillarHandler('ceph-salt:container:registries')
            }
        }
    },
    'system_update': {
        'help': '''
                System Update Options Configuration
                =========================================
                Options to control system updates
                ''',
        'options': {
            'packages': {
                'type': 'flag',
                'help': 'Update all packages',
                'handler': PillarHandler('ceph-salt:updates:enabled'),
                'default': True
            },
            'reboot': {
                'type': 'flag',
                'help': 'Reboot if needed',
                'handler': PillarHandler('ceph-salt:updates:reboot'),
                'default': True
            }
        }
    },
    'cephadm_bootstrap': {
        'help': '''
                Cluster Bootstrap Options Configuration
                =========================================
                Options to control the Ceph cluster bootstrap
                ''',
        'options': {
            'advanced': {
                'type': 'dict',
                'help': 'Cephadm bootstrap advanced arguments',
                'params_spec': {
                    'fsid': {},
                    'mon-id': {},
                    'mgr-id': {}
                },
                'default': {},
                'handler': PillarHandler('ceph-salt:bootstrap_arguments')
            },
            'ceph_conf': {
                'type': 'conf',
                'help': 'Bootstrap Ceph configuration',
                'default': [],
                'handler': PillarHandler('ceph-salt:bootstrap_ceph_conf')
            },
            'dashboard': {
                'type': 'group',
                'help': 'Dashboard settings',
                'options': {
                    'password': {
                        'default': None,
                        'default_text': 'randomly generated',
                        'sensitive': True,
                        'handler': PillarHandler('ceph-salt:dashboard:password')
                    },
                    'username': {
                        'default': 'admin',
                        'handler': PillarHandler('ceph-salt:dashboard:username')
                    }
                }
            },
            'mon_ip': {
                'help': 'Bootstrap Mon IP',
                'default': None,
                'handler': PillarHandler('ceph-salt:bootstrap_mon_ip')
            },
        }
    },
    'ssh': {
        'help': '''
                SSH Keys configuration
                ============================
                Options for configuring the SSH keys used by the SSH orchestrator
                ''',
        'handler': SSHGroupHandler(),
        'options': {
            'private_key': {
                'default': None,
                'help': "SSH RSA private key",
                'handler': SshPrivateKeyHandler()
            },
            'public_key': {
                'default': None,
                'help': "SSH RSA public key",
                'handler': SshPublicKeyHandler()
            },
        }
    },
    'time_server': {
        'help': '''
                Time Server Deployment Options
                ==============================
                Options to customize time server deployment and configuration.
                ''',
        'handler': TimeServerGroupHandler(),
        'options': {
            'external_servers': {
                'type': 'list',
                'default': [],
                'help': 'List of external NTP servers',
                'handler': PillarHandler('ceph-salt:time_server:external_time_servers')
            },
            'server_hostname': {
                'default': None,
                'help': 'FQDN of the time server node',
                'handler': TimeServerHandler(),
                'required': True
            },
            'subnet': {
                'default': None,
                'help': 'Subnet of the time server',
                'handler': TimeSubnetHandler(),
                'required': True
            },
        }
    },
}


class CephSaltRoot(configshell.ConfigNode):
    help_intro = '''
                 ceph-salt Configuration
                 =====================
                 This is a shell where you can manipulate ceph-salt's configuration.
                 Each configuration option is present under a configuration group.
                 You can navigate through the groups and options using the B{ls} and
                 B{cd} commands as in a typical shell.
                 In each path you can type B{help} to see the available commands.
                 Different options might have different commands available.
                 '''

    def __init__(self, shell):
        configshell.ConfigNode.__init__(self, '/', shell=shell)

    def list_commands(self):
        return tuple(['cd', 'ls', 'help', 'exit'])

    def summary(self):
        return "", None


class GroupNode(configshell.ConfigNode):
    def __init__(self, group_name, help, handler, parent):
        configshell.ConfigNode.__init__(self, group_name, parent)
        self.group_name = group_name
        self.help_intro = help
        self.handler = handler

        if self.handler:
            for cmd, func in self.handler.commands_map().items():
                setattr(self, 'ui_command_{}'.format(cmd), func)

    def list_commands(self):
        cmds = ['cd', 'ls', 'help', 'exit', 'reset', 'set']
        if self.handler:
            cmds.extend(list(self.handler.commands_map().keys()))
        return tuple(cmds)

    def summary(self):
        if self.handler:
            return self.handler.value()
        return "", None

    def ui_command_set(self, option_name, value):
        '''
        Sets the value of option
        '''
        self.get_child(option_name).ui_command_set(value)

    def ui_command_reset(self, option_name):
        '''
        Resets option value to the default
        '''
        self.get_child(option_name).ui_command_reset()


class OptionNode(configshell.ConfigNode):
    def __init__(self, option_name, option_dict, parent):
        configshell.ConfigNode.__init__(self, option_name, parent)
        self.option_name = option_name
        self.option_dict = option_dict
        self.help_intro = option_dict.get('help', ' ')
        self.value = None

    def _list_commands(self):
        return []

    def list_commands(self):
        cmds = ['cd', 'ls', 'help', 'exit', 'reset']
        cmds.extend(self._list_commands())
        return tuple(cmds)

    def _find_value(self):
        if self.value is None:
            value = None
            if 'handler' in self.option_dict:
                value, val_type = self.option_dict['handler'].value()
            if value is not None:
                if self.option_dict.get('sensitive', False):
                    return '***', None
                return value, val_type
            if 'default_text' in self.option_dict:
                val_type = None
                if self.option_dict.get('required', False):
                    val_type = False
                return self.option_dict['default_text'], val_type
            if 'default' in self.option_dict:
                return self.option_dict['default'], None
            raise Exception("No default value found for {}".format(self.option_name))
        return self.value, None

    def summary(self):
        value, val_type = self._find_value()
        if isinstance(value, bool):
            value = 'enabled' if value else 'disabled'
        if value is None and self.option_dict.get('required', False):
            return 'not set', False

        value_str = str(value)
        return value_str, val_type

    def ui_command_reset(self):
        '''
        Resets option value to the default
        '''
        if 'handler' in self.option_dict:
            self.option_dict['handler'].reset()
        else:
            self.value = None
        PP.pl_green('Value reset.')

    def _read_only(self):
        if 'handler' in self.option_dict:
            return self.option_dict['handler'].read_only()
        return False


class ValueOptionNode(OptionNode):
    def _list_commands(self):
        return ['set']

    def ui_command_set(self, value):
        '''
        Sets the value of option
        '''
        if self._read_only():
            raise Exception("Option {} cannot be modified".format(self.option_name))
        if 'handler' in self.option_dict:
            self.option_dict['handler'].save(value)
        else:
            self.value = value
        PP.pl_green('Value set.')

    def ui_complete_set(self, parameters, text, current_param):
        matching = []
        for value in self.option_dict['handler'].possible_values():
            if value.startswith(text):
                matching.append(value)
        return matching


class FlagOptionNode(OptionNode):
    def _list_commands(self):
        return ['enable', 'disable']

    def _set_option_value(self, bool_value):
        if self._read_only():
            raise Exception("Option {} cannot be modified".format(self.option_name))
        if 'handler' in self.option_dict:
            self.option_dict['handler'].save(bool_value)
        else:
            self.value = bool_value

    def ui_command_enable(self):
        '''
        Enables the option
        '''
        self._set_option_value(True)
        PP.pl_green('Enabled.')

    def ui_command_disable(self):
        '''
        Disables the option
        '''
        self._set_option_value(False)
        PP.pl_green('Disabled.')


class KeyValueNode(configshell.ConfigNode):
    def __init__(self, key, value, parent):
        configshell.ConfigNode.__init__(self, key, parent)
        self._value = str(value)

    def summary(self):
        return self._value, None


class ListElementNode(configshell.ConfigNode):
    def __init__(self, value, parent, child_data=None):
        configshell.ConfigNode.__init__(self, value, parent)
        if child_data is not None:
            if isinstance(child_data, dict):
                for k, v in child_data.items():
                    KeyValueNode(k, v, self)


class ListOptionNode(OptionNode):
    def __init__(self, option_name, option_dict, parent):
        super(ListOptionNode, self).__init__(option_name, option_dict, parent)
        value_list, _ = self._find_value()
        self.value = list(value_list)
        for value in value_list:
            ListElementNode(value, self)

    def _list_commands(self):
        return ['add', 'remove']

    def summary(self):
        value_list, _ = self._find_value()
        return str(len(value_list)) if value_list else 'empty', None

    def ui_command_add(self, value):
        if value not in self.value:
            self.value.append(value)
            self.option_dict['handler'].save(self.value)
            ListElementNode(value, self)
            PP.pl_green('Value added.')
        else:
            PP.pl_red('Value already exists.')

    def ui_command_remove(self, value):
        if value in self.value:
            self.value.remove(value)
            self.option_dict['handler'].save(self.value)
            self.remove_child(self.get_child(value))
            PP.pl_green('Value removed.')
        else:
            PP.pl_red('Value not found.')


class ListDictOptionNode(OptionNode):
    def __init__(self, option_name, option_dict, parent):
        super(ListDictOptionNode, self).__init__(option_name, option_dict, parent)
        value_list, _ = self._find_value()
        self.value = list(value_list)
        self._add_children(self.value)

    def _add_children(self, items):
        for idx, item in enumerate(items):
            ListElementNode(str(idx), self, item)

    def _remove_all_children(self):
        for idx in range(len(self.value)):
            self.remove_child(self.get_child(str(idx)))

    def _list_commands(self):
        return ['add', 'remove']

    def summary(self):
        value_list, _ = self._find_value()
        return str(len(value_list)) if value_list else 'empty', None

    def _precheck_params(self, spec, kwargs):
        # check required parameters
        all_params = set(spec.keys())
        required_params = {k for k, v in spec.items() if v.get('required', False)}
        assert required_params  # at least one parameter is required
        missing_params = required_params - set(kwargs.keys())
        if missing_params:
            raise ParamsException('Required parameter(s) are missing: {}.'.format(
                ', '.join(missing_params)))
        # check if there are any unknown parameters
        unknown_params = set(kwargs.keys()) - all_params
        if unknown_params:
            raise ParamsException(
                'Unknown parameter(s): {}.'.format(', '.join(unknown_params)))

    def _format_params(self, kwargs):
        """Validate and format input parameters."""
        spec = self.option_dict.get('params_spec')
        self._precheck_params(spec, kwargs)
        value = {}
        for k, v in kwargs.items():
            # validate the input value
            validator = spec[k].get('validator')
            if validator and not validator.validate(v):
                raise ParamsException("Invalid value for parameter {}: {}.".format(k, v))
            # transform to native type from str
            transformer = spec[k].get('transformer')
            value[k] = transformer.transform(v) if transformer else v
        return value

    def ui_command_add(self, **kwargs):
        new_item = self._format_params(kwargs)
        if new_item not in self.value:
            ListElementNode(str(len(self.value)), self, new_item)
            self.value.append(new_item)
            self.option_dict['handler'].save(self.value)
            PP.pl_green('Item added.')
        else:
            PP.pl_red('Item already exists.')

    def ui_command_remove(self, **kwargs):
        remove_item = self._format_params(kwargs)

        # do not include items that have specified KVs
        def __match(item):
            for search_k, search_v in remove_item.items():
                if item.get(search_k) != search_v:
                    break
            else:
                return True
            return False
        new_value = list(itertools.filterfalse(__match, self.value))

        remove_count = len(self.value) - len(new_value)
        if remove_count > 0:
            self._remove_all_children()
            self.value = new_value
            self._add_children(self.value)
            self.option_dict['handler'].save(self.value)
            PP.pl_green('{} item(s) removed.'.format(remove_count))
        else:
            PP.pl_red('Item not found.')

    def ui_command_reset(self):
        '''
        Resets option value to the default
        '''
        self._remove_all_children()
        super(ListDictOptionNode, self).ui_command_reset()


class DictElementNode(configshell.ConfigNode):
    def __init__(self, key, value, parent):
        configshell.ConfigNode.__init__(self, key, parent)
        self.value = value

    def summary(self):
        return self.value or ' ', None


class DictNode(OptionNode):
    def __init__(self, option_name, option_dict, parent):
        super(DictNode, self).__init__(option_name, option_dict, parent)
        value_dict, _ = self._find_value()
        self.value = dict(value_dict)
        for parameter, value in self.value.items():
            DictElementNode(parameter, value, self)

    def _list_commands(self):
        return ['set', 'remove']

    def summary(self):
        return '', None

    def ui_command_set(self, parameter, value):
        params_spec = self.option_dict.get('params_spec')
        if params_spec:
            if parameter not in params_spec:
                PP.pl_red("Invalid parameter '{}'. Valid parameters are {}".format(
                    parameter, list(params_spec.keys())))
                return
        child = None
        if parameter in self.value:
            child = self.get_child(parameter)
        self.value[parameter] = value
        self.option_dict['handler'].save(self.value)
        if child:
            child.value = value
        else:
            DictElementNode(parameter, value, self)
        PP.pl_green('Parameter set.')

    def ui_complete_set(self, parameters, text, current_param):
        matching = []
        params_spec = self.option_dict.get('params_spec')
        if params_spec:
            for param in params_spec.keys():
                if param.startswith(text):
                    matching.append(param)
        return matching

    def ui_command_remove(self, parameter):
        if parameter in self.value:
            self.value.pop(parameter)
            self.option_dict['handler'].save(self.value)
            self.remove_child(self.get_child(parameter))
            PP.pl_green('Parameter removed.')
        else:
            PP.pl_red('Parameter not found.')

    # pylint: disable=unused-argument
    def ui_complete_remove(self, parameters, text, current_param):
        matching = []
        for param in self.value:
            if param.startswith(text):
                matching.append(param)
        return matching

    def ui_command_reset(self):
        for key in self.value.keys():
            self.remove_child(self.get_child(key))
        self.value = {}
        self.option_dict['handler'].save(self.value)
        PP.pl_green('Parameters reset.')


class ConfOptionNode(OptionNode):
    def __init__(self, option_name, option_dict, parent):
        super(ConfOptionNode, self).__init__(option_name, option_dict, parent)
        value_dict, _ = self._find_value()
        self.value = dict(value_dict)
        for section in self.value.keys():
            self.add_child(section)

    def add_child(self, section):
        handler: PillarHandler = self.option_dict['handler']
        DictNode(section, {
            'handler': PillarHandler('{}:{}'.format(handler.pillar_path, section)),
            'default': {}
        }, self)

    def _list_commands(self):
        return ['add', 'remove']

    def summary(self):
        return '', None

    def ui_command_add(self, section):
        if section not in self.value:
            self.value[section] = {}
            self.option_dict['handler'].save(self.value)
            self.add_child(section)
            PP.pl_green('Section added.')
        else:
            PP.pl_red('Section already exists.')

    def ui_command_remove(self, section):
        if section in self.value:
            self.value.pop(section)
            self.option_dict['handler'].save(self.value)
            self.remove_child(self.get_child(section))
            PP.pl_green('Section removed.')
        else:
            PP.pl_red('Section not found.')

    def ui_command_reset(self):
        for key in self.value.keys():
            self.remove_child(self.get_child(key))
        self.value = {}
        self.option_dict['handler'].save(self.value)
        PP.pl_green('Config reset.')


class MinionOptionNode(configshell.ConfigNode):
    def __init__(self, minion, handler, parent):
        configshell.ConfigNode.__init__(self, minion, parent)
        self.handler = handler

    def summary(self):
        if self.handler:
            return self.handler.value()
        return "", None


class MinionsOptionNode(OptionNode):
    def __init__(self, option_name, option_dict, parent):
        super(MinionsOptionNode, self).__init__(option_name, option_dict, parent)
        value_list, _ = self._find_value()
        self.value = list(value_list)
        for value in value_list:
            MinionOptionNode(value, option_dict['handler'].children_handler(value), self)

    def _list_commands(self):
        return ['add', 'remove']

    def summary(self):
        value_list, val_type = self._find_value()
        if value_list:
            return "Minions: {}".format(str(len(value_list))), val_type
        return 'no minions', False

    def ui_command_add(self, minion_id):
        matching = fnmatch.filter(self.option_dict['handler'].possible_values(), minion_id)
        counter = 0
        has_errors = False
        for match in matching:
            if match not in self.value:
                new_value = list(self.value)
                new_value.append(match)
                try:
                    self.option_dict['handler'].save(new_value)
                    self.value = new_value
                    MinionOptionNode(match, self.option_dict['handler'].children_handler(match),
                                     self)
                    counter += 1
                except CephSaltException as ex:
                    logger.exception(ex)
                    PP.pl_red(ex)
                    has_errors = True
        if counter == 1:
            PP.pl_green('1 minion added.')
        elif counter > 1:
            PP.pl_green('{} minions added.'.format(counter))
        elif not has_errors:
            PP.pl_red('No minions matched "{}".'.format(minion_id))

    def ui_command_remove(self, minion_id):
        matching = fnmatch.filter(self.value, minion_id)
        counter = 0
        has_errors = False
        for match in matching:
            new_value = list(self.value)
            new_value.remove(match)
            try:
                self.option_dict['handler'].save(new_value)
                self.value = new_value
                self.remove_child(self.get_child(match))
                counter += 1
            except CephSaltException as ex:
                logger.exception(ex)
                PP.pl_red(ex)
                has_errors = True
        if counter == 1:
            PP.pl_green('1 minion removed.')
        elif counter > 1:
            PP.pl_green('{} minions removed.'.format(counter))
        elif not has_errors:
            PP.pl_red('No minions matched "{}".'.format(minion_id))

    # pylint: disable=unused-argument
    def ui_complete_add(self, parameters, text, current_param):
        matching = []
        for minion in self.option_dict['handler'].possible_values():
            if minion.startswith(text):
                matching.append(minion)
        return matching

    def ui_complete_remove(self, parameters, text, current_param):
        matching = []
        for minion in self.value:
            if minion.startswith(text):
                matching.append(minion)
        return matching


def _generate_option_node(option_name, option_dict, parent):
    if option_dict.get('type', None) == 'group':
        _generate_group_node(option_name, option_dict, parent)
        return

    if 'options' in option_dict:
        raise Exception("Invalid option node {}".format(option_name))

    if option_dict.get('type', None) == 'flag':
        FlagOptionNode(option_name, option_dict, parent)
    elif option_dict.get('type', None) == 'list':
        ListOptionNode(option_name, option_dict, parent)
    elif option_dict.get('type', None) == 'list_dict':
        ListDictOptionNode(option_name, option_dict, parent)
    elif option_dict.get('type', None) == 'dict':
        DictNode(option_name, option_dict, parent)
    elif option_dict.get('type', None) == 'conf':
        ConfOptionNode(option_name, option_dict, parent)
    elif option_dict.get('type', None) == 'minions':
        MinionsOptionNode(option_name, option_dict, parent)
    else:
        ValueOptionNode(option_name, option_dict, parent)


def _generate_group_node(group_name, group_dict, parent):
    group_node = GroupNode(group_name, group_dict.get('help', ""), group_dict.get('handler', None),
                           parent)
    for option_name, option_dict in group_dict['options'].items():
        _generate_option_node(option_name, option_dict, group_node)


def generate_config_shell_tree(shell):
    root_node = CephSaltRoot(shell)
    for group_name, group_dict in CEPH_SALT_OPTIONS.items():
        _generate_group_node(group_name, group_dict, root_node)


class CephSaltConfigShell(configshell.ConfigShell):
    # pylint: disable=anomalous-backslash-in-string
    def __init__(self):
        super(CephSaltConfigShell, self).__init__(
            '~/.ceph_salt_config_shell')
        # Grammar of the command line
        command = locatedExpr(Word(alphanums + '_'))('command')
        var = QuotedString('"') | QuotedString("'") | Word(alphanums + '?;&*$!#,=_\+/.<>()~@:-%[]')
        value = var
        keyword = Word(alphanums + '_\-')
        kparam = locatedExpr(keyword + Suppress('=') + Optional(value, default=''))('kparams*')
        pparam = locatedExpr(var)('pparams*')
        parameter = kparam | pparam
        parameters = OneOrMore(parameter)
        bookmark = Regex('@([A-Za-z0-9:_.]|-)+')
        pathstd = Regex('([A-Za-z0-9:_.\[\]]|-)*' + '/' + '([A-Za-z0-9:_.\[\]/]|-)*') | '..' | '.'
        path = locatedExpr(bookmark | pathstd | '*')('path')
        parser = Optional(path) + Optional(command) + Optional(parameters)
        self._parser = parser


def check_config_prerequesites():
    try:
        check_salt_master_status()
        return True
    except CephSaltPillarNotConfigured:
        try:
            PillarManager.install_pillar()
            return True
        except PillarFileNotPureYaml:
            PP.println("""
ceph-salt pillar file is not installed yet, and we can't add it automatically
because pillar's top.sls is probably using Jinja2 expressions.
Please create a ceph-salt.sls file in salt's pillar directory with the following
content:

ceph-salt: {}

and add the following pillar configuration to top.sls file:

base:
  'ceph-salt:member':
    - match: grain
    - ceph-salt
""")
    return False


def count_hosts(host_ls):
    all_nodes = PillarManager.get('ceph-salt:minions:all')
    if all_nodes is None:
        all_nodes = []
    deployed = []
    not_managed = []
    for host in host_ls:
        if host['hostname'] in all_nodes:
            deployed.append(host)
        else:
            not_managed.append(host)
    return (len(all_nodes), len(deployed), len(not_managed))


def run_status():
    if not check_config_prerequesites():
        return False
    status = {}
    result = True
    host_ls = CephOrch.host_ls()
    ceph_salt_nodes, deployed_nodes, not_managed_nodes = count_hosts(host_ls)
    status['hosts'] = '{}/{} managed by cephadm'.format(deployed_nodes, ceph_salt_nodes)
    if not_managed_nodes:
        status['hosts'] += ' ({} hosts not managed by cephsalt)'.format(not_managed_nodes)
    error_msg = validate_config(host_ls)
    if error_msg:
        result = False
        logger.info(error_msg)
        status['config'] = PP.red(error_msg)
    else:
        status['config'] = PP.green("OK")
    for k, v in status.items():
        PP.println('{}{}'.format('{}: '.format(k).ljust(8), v))
    return result


def run_config_shell():
    if not check_config_prerequesites():
        return False
    shell = CephSaltConfigShell()
    generate_config_shell_tree(shell)
    while True:
        try:
            shell.run_interactive()
            break
        except (configshell.ExecutionError, CephSaltException) as ex:
            logger.exception(ex)
            PP.pl_red(ex)
    return True


def run_config_cmdline(cmdline):
    if not check_config_prerequesites():
        return False
    shell = CephSaltConfigShell()
    generate_config_shell_tree(shell)
    logger.info("running command: %s", cmdline)
    shell.run_cmdline(cmdline)
    return True


def run_export(pretty):
    config = PillarManager.get('ceph-salt')
    if pretty:
        PP.println(json.dumps(config, indent=4, sort_keys=True))
    else:
        PP.println(json.dumps(config))
    return True


def _get_salt_minions_by_host():
    salt_minions_by_host = {}
    minions = SaltClient.caller().cmd('minion.list')['minions']
    for minion_id in minions:
        short_name = minion_id.split('.', 1)[0]
        salt_minions_by_host[short_name] = minion_id
    return salt_minions_by_host


def run_import(config_file):
    with open(config_file) as json_file:
        config = json.load(json_file)
    salt_minions_by_host = _get_salt_minions_by_host()
    minions_config = config.get('minions', {})
    # Validate
    for host in minions_config.get('all', []):
        if host not in salt_minions_by_host:
            PP.pl_red("Cannot find host '{}'".format(host))
            return False
    # Update pillar
    PillarManager.set('ceph-salt', config)
    # Update grains
    minions = GrainsManager.filter_by('ceph-salt', 'member')
    if minions:
        GrainsManager.del_grain(minions, 'ceph-salt')
    for host in minions_config.get('all', []):
        node = CephNode(salt_minions_by_host[host])
        if host in minions_config.get('admin', []):
            node.add_role('admin')
        node.save()
    PP.pl_green('Configuration imported.')
    return True
