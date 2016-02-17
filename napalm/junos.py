# Copyright 2015 Spotify AB. All rights reserved.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

import re
import collections

from napalm.utils import junos_views
from base import NetworkDriver

from jnpr.junos import Device
from jnpr.junos.utils.config import Config
from jnpr.junos.exception import ConfigLoadError
from exceptions import ReplaceConfigException, MergeConfigException, CommandErrorException

from lxml import etree as ET

from exceptions import ReplaceConfigException, MergeConfigException

from utils import string_parsers


class JunOSDriver(NetworkDriver):

    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        self.config_replace = False

        if optional_args is None:
            optional_args = {}
        self.port = optional_args.get('port', 22)

        self.device = Device(hostname, user=username, password=password, port=self.port)

    def open(self):
        self.device.open()
        self.device.timeout = self.timeout
        self.device.bind(cu=Config)
        self.device.cu.lock()

    def close(self):
        self.device.cu.unlock()
        self.device.close()

    def _load_candidate(self, filename, config, overwrite):
        if filename is None:
            configuration = config
        else:
            with open(filename) as f:
                configuration = f.read()

        try:
            self.device.cu.load(configuration, format='text', overwrite=overwrite)
        except ConfigLoadError as e:
            if self.config_replace:
                raise ReplaceConfigException(e.message)
            else:
                raise MergeConfigException(e.message)

    def load_replace_candidate(self, filename=None, config=None):
        self.config_replace = True
        self._load_candidate(filename, config, True)

    def load_merge_candidate(self, filename=None, config=None):
        self.config_replace = False
        self._load_candidate(filename, config, False)

    def compare_config(self):
        diff = self.device.cu.diff()

        if diff is None:
            return ''
        else:
            return diff.strip()

    def commit_config(self):
        self.device.cu.commit()

    def discard_config(self):
        self.device.cu.rollback(rb_id=0)

    def rollback(self):
        self.device.cu.rollback(rb_id=1)
        self.commit_config()

    def get_facts(self):

        output = self.device.facts

        uptime = 0
        if 'RE0' in output:
            uptime = output['RE0']['up_time']

        interfaces = junos_views.junos_iface_table(self.device)
        interfaces.get()
        interface_list = interfaces.keys()

        return {
            'vendor': u'Juniper',
            'model': unicode(output['model']),
            'serial_number': unicode(output['serialnumber']),
            'os_version': unicode(output['version']),
            'hostname': unicode(output['hostname']),
            'fqdn': unicode(output['fqdn']),
            'uptime': string_parsers.convert_uptime_string_seconds(uptime),
            'interface_list': interface_list
        }

    def get_interfaces(self):

        # init result dict
        result = {}

        interfaces = junos_views.junos_iface_table(self.device)
        interfaces.get()

        # convert all the tuples to our pre-defined dict structure
        for iface in interfaces.keys():
            result[iface] = {
                'is_up': interfaces[iface]['is_up'],
                'is_enabled': interfaces[iface]['is_enabled'],
                'description': interfaces[iface]['description'] or u'',
                'last_flapped': interfaces[iface]['last_flapped'] or -1,
                'mac_address': unicode(interfaces[iface]['mac_address'])
            }
            result[iface]['last_flapped'] = float(result[iface]['last_flapped'])

            match = re.search(r'\d+', interfaces[iface]['speed'] or '')
            if match is not None:
                result[iface]['speed'] = int(match.group(0))
            else:
                result[iface]['speed'] = -1

        return result

    def get_interfaces_counters(self):
        query = junos_views.junos_iface_counter_table(self.device)
        query.get()
        interface_counters = dict()
        for interface, counters in query.items():
            interface_counters[interface] = {k: v if v is not None else -1 for k, v in counters}
        return interface_counters

    def get_environment(self):
        environment = junos_views.junos_enviroment_table(self.device)
        routing_engine = junos_views.junos_routing_engine_table(self.device)
        temperature_thresholds = junos_views.junos_temperature_thresholds(self.device)
        environment.get()
        routing_engine.get()
        temperature_thresholds.get()
        environment_data = dict()

        for sensor_object, object_data in environment.items():
            structured_object_data = {k: v for k, v in object_data}

            if structured_object_data['class'] == 'Power':
                # Create a dict for the 'power' key
                try:
                    environment_data['power'][sensor_object] = dict()
                except KeyError:
                    environment_data['power'] = dict()
                    environment_data['power'][sensor_object] = dict()

                # Set these values to -1, because Junos does not provide them
                environment_data['power'][sensor_object]['capacity'] = -1.0
                environment_data['power'][sensor_object]['output'] = -1.0

            if structured_object_data['class'] == 'Fans':
                # Create a dict for the 'fans' key
                try:
                    environment_data['fans'][sensor_object] = dict()
                except KeyError:
                    environment_data['fans'] = dict()
                    environment_data['fans'][sensor_object] = dict()

            if structured_object_data['status'] == 'OK' and structured_object_data['class'] == 'Power':
                # If status is Failed, Absent or Testing, set status to False.
                environment_data['power'][sensor_object]['status'] = True

            elif structured_object_data['status'] != 'OK' and structured_object_data['class'] == 'Power':
                environment_data['power'][sensor_object]['status'] = False

            elif structured_object_data['status'] == 'OK' and structured_object_data['class'] == 'Fans':
                # If status is Failed, Absent or Testing, set status to False.
                environment_data['fans'][sensor_object]['status'] = True

            elif structured_object_data['status'] != 'OK' and structured_object_data['class'] == 'Fans':
                environment_data['fans'][sensor_object]['status'] = False

            for temperature_object, temperature_data in temperature_thresholds.items():
                structured_temperature_data = {k: v for k, v in temperature_data}
                if structured_object_data['class'] == 'Temp':
                    # Create a dict for the 'temperature' key
                    try:
                        environment_data['temperature'][sensor_object] = dict()
                    except KeyError:
                        environment_data['temperature'] = dict()
                        environment_data['temperature'][sensor_object] = dict()

                    environment_data['temperature'][sensor_object]['temperature'] = float(structured_object_data['temperature'])
                    # Set a default value (False) to the key is_critical and is_alert
                    environment_data['temperature'][sensor_object]['is_alert'] = False
                    environment_data['temperature'][sensor_object]['is_critical'] = False
                    # Check if the working temperature is equal to or higher than alerting threshold
                    if structured_temperature_data['red-alarm'] <= structured_object_data['temperature']:
                        environment_data['temperature'][sensor_object]['is_critical'] = True
                        environment_data['temperature'][sensor_object]['is_alert'] = True
                    elif structured_temperature_data['yellow-alarm'] <= structured_object_data['temperature']:
                        environment_data['temperature'][sensor_object]['is_alert'] = True

        for routing_engine_object, routing_engine_data in routing_engine.items():
            structured_routing_engine_data = {k: v for k, v in routing_engine_data}
            # Create dicts for 'cpu' and 'memory'.
            try:
                environment_data['cpu'] = dict()
                environment_data['cpu'][routing_engine_object] = dict()
                environment_data['memory'] = dict()
            except KeyError:
                environment_data['cpu'] = dict()
                environment_data['cpu'][routing_engine_object] = dict()
                environment_data['memory'] = dict()
            # Calculate the CPU usage by using the CPU idle value.
            environment_data['cpu'][routing_engine_object]['%usage'] = 100.0 - structured_routing_engine_data['cpu-idle']
            environment_data['memory']['available_ram'] = structured_routing_engine_data['memory-dram-size']
            # Junos gives us RAM in %, so calculation has to be made.
            # Sadly, bacause of this, results are not 100% accurate to the truth.
            environment_data['memory']['used_ram'] = (structured_routing_engine_data['memory-dram-size'] / 100 * structured_routing_engine_data['memory-buffer-utilization'])

        return environment_data

    @staticmethod
    def _get_address_family(table):
        """
        Function to derive address family from a junos table name
        :params table: The name of the routing table
        :returns: address family
        """
        address_family_mapping = {
            'inet': 'ipv4',
            'inet6': 'ipv6'
        }
        family = table.split('.')[-2]
        return address_family_mapping[family]

    def _parse_route_stats(self, neighbor):
        data = {}
        if not neighbor['is_up']:
            pass
        elif isinstance(neighbor['tables'], list):
            for idx, table in enumerate(neighbor['tables']):
                family = self._get_address_family(table)
                data[family] = {}
                data[family]['received_prefixes'] = neighbor['received_prefixes'][idx]
                data[family]['accepted_prefixes'] = neighbor['accepted_prefixes'][idx]
                data[family]['sent_prefixes'] = neighbor['sent_prefixes'][idx]
        else:
            family = self._get_address_family(neighbor['tables'])
            data[family] = {}
            data[family]['received_prefixes'] = neighbor['received_prefixes']
            data[family]['accepted_prefixes'] = neighbor['accepted_prefixes']
            data[family]['sent_prefixes'] = neighbor['sent_prefixes']
        return data

    @staticmethod
    def _parse_value(value):
        if isinstance(value, basestring):
            return unicode(value)
        elif value is None:
            return u''
        else:
            return value

    def get_bgp_neighbors(self):
        instances = junos_views.junos_route_instance_table(self.device)
        uptime_table = junos_views.junos_bgp_uptime_table(self.device)
        bgp_neighbors = junos_views.junos_bgp_table(self.device)
        keys =['local_as', 'remote_as', 'is_up', 'is_enabled', 'description', 'remote_id']
        bgp_neighbor_data = {}
        for instance, instance_data in instances.get().items():
            if instance.startswith('__'):
                # junos internal instances
                continue
            instance_name = "global" if instance == 'master' else instance
            bgp_neighbor_data[instance_name] = {'peers': {}}
            for neighbor, data in bgp_neighbors.get(instance=instance).items():
                neighbor_data = {k: v for k, v in data}
                peer_ip = neighbor.split('+')[0]
                if 'router_id' not in bgp_neighbor_data[instance_name]:
                    # we only need to set this once
                    bgp_neighbor_data[instance_name]['router_id'] = unicode(neighbor_data['local_id'])
                peer = {key:self._parse_value(value) for key, value in neighbor_data.iteritems() if key in keys}
                peer['address_family'] = self._parse_route_stats(neighbor_data)
                bgp_neighbor_data[instance_name]['peers'][peer_ip] = peer
            for neighbor, uptime in uptime_table.get(instance=instance).items():
                bgp_neighbor_data[instance_name]['peers'][neighbor]['uptime'] = uptime[0][1]
        for key in bgp_neighbor_data.keys():
            if not bgp_neighbor_data[key]['peers']:
                del bgp_neighbor_data[key]
        return bgp_neighbor_data

    def get_lldp_neighbors(self):
        lldp = junos_views.junos_lldp_table(self.device)
        lldp.get()

        result = lldp.items()

        neighbors = dict()
        for neigh in result:
            if neigh[0] not in neighbors.keys():
                neighbors[neigh[0]] = list()
            neighbors[neigh[0]].append({x[0]: unicode(x[1]) for x in neigh[1]})

        return neighbors

    def get_lldp_neighbors_detail(self, interface = ''):

        lldp_neighbors = dict()

        lldp_table = junos_views.junos_lldp_neighbors_detail_table(self.device)
        lldp_table.get()

        lldp_items = lldp_table.items()

        for lldp_item in lldp_items:
            interface = lldp_item[0]
            if interface not in lldp_neighbors.keys():
                lldp_neighbors[interface] = list()
            lldp_neighbors[interface].append(
                {elem[0]: elem[1] for elem in lldp_item[1]}
            )

        return lldp_neighbors

    def cli(self, commands = None):

        cli_output = dict()

        if type(commands) is not list:
            raise TypeError('Please enter a valid list of commands!')

        for command in commands:
            try:
                cli_output[unicode(command)] = unicode(self.device.cli(command))
            except Exception as e:
                cli_output[unicode(command)] = 'Unable to execute command "{cmd}": {err}'.format(
                    cmd = command,
                    err = e
                )
                raise CommandErrorException(str(cli_output))

        return cli_output

    @staticmethod
    def _convert(to, who, default = u''):
        try:
            return to(who)
        except:
            return default

    def get_bgp_config(self, group = '', neighbor = ''):

        def update_dict(d, u): # for deep dictionary update
            for k, v in u.iteritems():
                if isinstance(d, collections.Mapping):
                    if isinstance(v, collections.Mapping):
                        r = update_dict(d.get(k, {}), v)
                        d[k] = r
                    else:
                        d[k] = u[k]
                else:
                    d = {k: u[k]}
            return d

        def build_prefix_limit(**args):

            """
            This helper will transform the lements of a dictionary into nested dictionaries:
            Example:

                {
                    'inet_unicast_limit': 500,
                    'inet_unicast_teardown_threshold': 95,
                    'inet_unicast_teardown_timeout': 5
                }

                becomes:

                {
                    'inet': {
                        'unicast': {
                            'limit': 500,
                            'teardown': {
                                'threshold': 95,
                                'timeout': 5
                            }
                        }
                    }
                }

            """

            prefix_limit = dict()

            for key, value in args.iteritems():
                key_levels = key.split('_')
                length     = len(key_levels)-1
                temp_dict = {
                    key_levels[length]: value
                }
                for index in reversed(range(length)):
                    level = key_levels[index]
                    temp_dict = {level: temp_dict}
                update_dict(prefix_limit, temp_dict)

            return prefix_limit

        _COMMON_FIELDS_DATATYPE_ = {
            'description'                                   : unicode,
            'local_address'                                 : unicode,
            'local_as'                                      : int,
            'peer_as'                                       : int,
            'import_policy'                                 : unicode,
            'export_policy'                                 : unicode,
            'inet_unicast_limit_prefix_limit'               : int,
            'inet_unicast_teardown_threshold_prefix_limit'  : int,
            'inet_unicast_teardown_timeout_prefix_limit'    : int,
            'inet_unicast_novalidate_prefix_limit'          : int,
            'inet_flow_limit_prefix_limit'                  : int,
            'inet_flow_teardown_threshold_prefix_limit'     : int,
            'inet_flow_teardown_timeout_prefix_limit'       : int,
            'inet_flow_novalidate_prefix_limit'             : unicode,
            'inet6_unicast_limit_prefix_limit'              : int,
            'inet6_unicast_teardown_threshold_prefix_limit' : int,
            'inet6_unicast_teardown_timeout_prefix_limit'   : int,
            'inet6_unicast_novalidate_prefix_limit'         : int,
            'inet6_flow_limit_prefix_limit'                 : int,
            'inet6_flow_teardown_threshold_prefix_limit'    : int,
            'inet6_flow_teardown_timeout_prefix_limit'      : int,
            'inet6_flow_novalidate_prefix_limit'            : unicode,
        }

        _PEER_FIELDS_DATATYPE_MAP_ = {
            'group'             : unicode,
            'authentication_key': unicode,
            'route_reflector'   : bool,
            'nhs'               : bool
        }
        _PEER_FIELDS_DATATYPE_MAP_.update(
            _COMMON_FIELDS_DATATYPE_
        )

        _GROUP_FIELDS_DATATYPE_MAP_ = {
            'type'              : unicode,
            'apply_groups'      : list,
            'remove_private'    : bool,
            'multipath'         : bool,
            'multihop_ttl'      : int
        }
        _GROUP_FIELDS_DATATYPE_MAP_.update(
            _COMMON_FIELDS_DATATYPE_
        )

        _DATATYPE_DEFAULT_ = {
            unicode     : u'',
            int         : 0,
            bool        : False,
            list        : []
        }

        bgp_config = dict()

        if group:
            bgp = junos_views.junos_bgp_config_group_table(self.device)
            bgp.get(group = group)
        else:
            bgp = junos_views.junos_bgp_config_table(self.device)
            bgp.get()
            neighbor = '' # if no group is set, no neighbor should be set either
        bgp_items = bgp.items()

        peers = junos_views.junos_bgp_config_peers_table(self.device)
        peers.get() # unfortunately cannot add filters for group name of neighbor address
        peers_items = peers.items()

        bgp_neighbors = dict()

        for bgp_group_neighbor in peers_items:
            bgp_peer_address  = bgp_group_neighbor[0]
            if neighbor and bgp_peer_address != neighbor:
                continue # if filters applied, jump over all other neighbors
            bgp_group_details = bgp_group_neighbor[1]
            bgp_peer_details  = {field: _DATATYPE_DEFAULT_.get(datatype) for field, datatype in _PEER_FIELDS_DATATYPE_MAP_.iteritems() if '_prefix_limit' not in field}
            for elem in bgp_group_details:
                if '_prefix_limit' not in elem[0] and elem[1] is not None:
                    datatype = _PEER_FIELDS_DATATYPE_MAP_.get(elem[0])
                    default  = _DATATYPE_DEFAULT_.get(datatype)
                    bgp_peer_details.update({
                        elem[0]: self._convert(datatype, elem[1], default)
                    })
            prefix_limit_fields = dict()
            for elem in bgp_group_details:
                if '_prefix_limit' in elem[0] and elem[1] is not None:
                    datatype = _PEER_FIELDS_DATATYPE_MAP_.get(elem[0])
                    default  = _DATATYPE_DEFAULT_.get(datatype)
                    prefix_limit_fields.update({
                        elem[0].replace('_prefix_limit', ''): self._convert(datatype, elem[1], default)
                    })
            bgp_peer_details['prefix_limit'] = build_prefix_limit(**prefix_limit_fields)
            # and all these things only because PyEZ cannto convert to a specifc datatype when retrieving config...
            group = bgp_peer_details.pop('group')
            if group not in bgp_neighbors.keys():
                bgp_neighbors[group] = dict()
            bgp_neighbors[group][bgp_peer_address] = bgp_peer_details
            if neighbor and bgp_peer_address == neighbor:
                break # found the desired neighbor

        for bgp_group in bgp_items:
            bgp_group_name    = bgp_group[0]
            bgp_group_details = bgp_group[1]
            bgp_config[bgp_group_name] = {field: _DATATYPE_DEFAULT_.get(datatype) for field, datatype in _GROUP_FIELDS_DATATYPE_MAP_.iteritems() if '_prefix_limit' not in field}
            for elem in bgp_group_details:
                if '_prefix_limit' not in elem[0] and elem[1] is not None:
                    datatype = _GROUP_FIELDS_DATATYPE_MAP_.get(elem[0])
                    default  = _DATATYPE_DEFAULT_.get(datatype)
                    bgp_config[bgp_group_name].update({
                        elem[0]: self._convert(datatype, elem[1], default)
                    })
            prefix_limit_fields = dict()
            for elem in bgp_group_details:
                if '_prefix_limit' in elem[0] and elem[1] is not None:
                    datatype = _GROUP_FIELDS_DATATYPE_MAP_.get(elem[0])
                    default  = _DATATYPE_DEFAULT_.get(datatype)
                    prefix_limit_fields.update({
                        elem[0].replace('_prefix_limit', ''): self._convert(datatype, elem[1], default)
                    })
            print prefix_limit_fields
            bgp_config[bgp_group_name]['prefix_limit'] = build_prefix_limit(**prefix_limit_fields)
            bgp_config[bgp_group_name]['neighbors'] = bgp_neighbors.get(bgp_group_name, {})

        return bgp_config