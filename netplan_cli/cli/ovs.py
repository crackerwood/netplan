#!/usr/bin/python3
#
# Copyright (C) 2020 Canonical, Ltd.
# Author: Lukas 'slyon' Märdian <lukas.maerdian@canonical.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 3.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import subprocess

OPENVSWITCH_OVS_VSCTL = 'ovs-vsctl'
# Defaults for non-optional settings, as defined here:
# http://www.openvswitch.org/ovs-vswitchd.conf.db.5.pdf
DEFAULTS = {
    # Mandatory columns:
    'mcast_snooping_enable': 'false',
    'rstp_enable': 'false',
}
GLOBALS = {
    # Global commands:
    'set-ssl': ('del-ssl', 'get-ssl'),
    'set-fail-mode': ('del-fail-mode', 'get-fail-mode'),
    'set-controller': ('del-controller', 'get-controller'),
}


class OvsDbServerNotRunning(Exception):
    pass


class OvsDbServerNotInstalled(Exception):
    pass


def _ovs_vsctl_path():  # pragma: nocover
    if os.path.exists('/snap/bin/'+OPENVSWITCH_OVS_VSCTL):
        return '/snap/bin/'+OPENVSWITCH_OVS_VSCTL
    else:
        return '/usr/bin/'+OPENVSWITCH_OVS_VSCTL


OVS_VSCTL_PATH = _ovs_vsctl_path()


def _ovs_installed():  # pragma: nocover
    return os.path.exists(OVS_VSCTL_PATH)


def _ovs_active():  # pragma: nocover
    return subprocess.call([OVS_VSCTL_PATH, 'show'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def _del_col(type, iface, column, value):
    """Cleanup values from a column (i.e. "column=value")"""
    default = DEFAULTS.get(column)
    if default is None:
        # removes the exact value only if it was set by netplan
        cmd = [OVS_VSCTL_PATH, 'remove', type, iface, column, value]
        logging.debug('Running: %s' % ' '.join(cmd))
        subprocess.check_call(cmd)
    elif default and default != value:
        # reset to default, if its not the default already
        cmd = [OVS_VSCTL_PATH, 'set', type, iface, '%s=%s' % (column, default)]
        logging.debug('Running: %s' % ' '.join(cmd))
        subprocess.check_call(cmd)


def _del_dict(type, iface, column, key, value):
    """Cleanup values from a dictionary (i.e. "column:key=value")"""
    # removes the exact value only if it was set by netplan
    cmd = [OVS_VSCTL_PATH, 'remove', type, iface, column, '%s=\"%s\"' % (key, value)]
    logging.debug('Running: %s' % ' '.join(cmd))
    subprocess.check_call(cmd)


def _del_global(type, iface, key, value):
    """Cleanup commands from the global namespace"""
    del_cmd, get_cmd = GLOBALS.get(key, (None, None))
    if del_cmd == 'del-ssl':
        iface = None

    if del_cmd:
        args_get = [OVS_VSCTL_PATH, get_cmd]
        args_del = [OVS_VSCTL_PATH, del_cmd]
        if iface:
            args_get.append(iface)
            args_del.append(iface)
        # Check the current value of a global command and compare it to the tag-value, e.g.:
        # * get-ssl: netplan/global/set-ssl=/private/key.pem,/another/cert.pem,/some/ca-cert.pem
        # Private key: /private/key.pem
        # Certificate: /another/cert.pem
        # CA Certificate: /some/ca-cert.pem
        # Bootstrap: false
        # * get-fail-mode: netplan/global/set-fail-mode=secure
        # secure
        # * get-controller: netplan/global/set-controller=tcp:127.0.0.1:1337,unix:/some/socket
        # tcp:127.0.0.1:1337
        # unix:/some/socket
        out = subprocess.check_output(args_get, text=True)
        # Clean it only if the exact same value(s) were set by netplan.
        # Don't touch it if other values were set by another integration.
        if all(item in out for item in value.split(',')):
            subprocess.check_call(args_del)
    else:
        raise Exception('Reset command unknown for:', key)


def clear_setting(type, iface, setting, value):
    """Check if this setting is in a dict or a colum and delete accordingly"""
    split = setting.split('/', 2)
    col = split[1]
    if col == 'global' and len(split) > 2:
        _del_global(type, iface, split[2], value)
    elif len(split) > 2:
        _del_dict(type, iface, split[1], split[2], value)
    else:
        _del_col(type, iface, split[1], value)
    # Cleanup the tag itself (i.e. "netplan/column[/key]")
    subprocess.check_call([OVS_VSCTL_PATH, 'remove', type, iface, 'external-ids', setting])


def is_ovs_interface(iface, np_interface_dict):
    assert isinstance(np_interface_dict, dict)
    np_def = np_interface_dict.get(iface, None)
    return np_def and np_def.backend == 'OpenVSwitch'


def apply_ovs_cleanup(config_manager, ovs_old, ovs_current):  # pragma: nocover (covered in autopkgtest)
    """
    Query OpenVSwitch state through 'ovs-vsctl' and filter for netplan=true
    tagged ports/bonds and bridges. Delete interfaces which are not defined
    in the current configuration.
    Also filter for individual settings tagged netplan/<column>[/<key]=value
    in external-ids and clear them if they have been set by netplan.
    """
    if not _ovs_installed():
        raise OvsDbServerNotInstalled("Cannot apply OpenvSwitch cleanup: ovs-vsctl is 'not-found'")

    if not _ovs_active():
        raise OvsDbServerNotRunning('OpenvSwitch database is not running')

    config_manager.parse()
    ovs_ifaces = set()
    for i in config_manager.netdefs.keys():
        if (is_ovs_interface(i, config_manager.netdefs)):
            ovs_ifaces.add(i)

    # Tear down old OVS interfaces, not defined in the current config.
    # Use 'del-br' on the Interface table, to delete any netplan created VLAN fake bridges.
    # Use 'del-bond-iface' on the Interface table, to delete netplan created patch port interfaces
    # Step 1: Delete all interfaces, which are not part of the current OVS config
    for t in (('Port', 'del-port'), ('Bridge', 'del-br'), ('Interface', 'del-br')):
        out = subprocess.check_output([OVS_VSCTL_PATH, '--columns=name,external-ids',
                                       '-f', 'csv', '-d', 'bare', '--no-headings', 'list', t[0]],
                                      text=True)
        for line in out.splitlines():
            if 'netplan=true' in line:
                iface = line.split(',')[0]
                # Skip cleanup if this OVS interface is part of the current netplan OVS config
                if iface in ovs_ifaces:
                    continue
                if t[0] == 'Interface' and subprocess.run([OVS_VSCTL_PATH, 'br-exists', iface]).returncode > 0:
                    subprocess.check_call([OVS_VSCTL_PATH, '--if-exists', 'del-bond-iface', iface])
                else:
                    subprocess.check_call([OVS_VSCTL_PATH, '--if-exists', t[1], iface])

    # Step 2: Clean up the settings of the remaining interfaces
    for t in ('Port', 'Bridge', 'Interface', 'Open_vSwitch', 'Controller'):
        cols = 'name,external-ids'
        if t == 'Open_vSwitch':
            cols = 'external-ids'
        elif t == 'Controller':
            cols = '_uuid,external-ids'  # handle _uuid as if it would be the iface 'name'
        out = subprocess.check_output([OVS_VSCTL_PATH, '--columns=%s' % cols,
                                       '-f', 'csv', '-d', 'bare', '--no-headings', 'list', t],
                                      text=True)
        for line in out.splitlines():
            if 'netplan/' in line:
                iface = '.'
                extids = line
                if t != 'Open_vSwitch':
                    iface, extids = line.split(',', 1)
                # Check each line (interface) if it contains any netplan tagged settings, e.g.:
                # ovs0,"iface-id=myhostname netplan=true netplan/external-ids/iface-id=myhostname"
                # ovs1,"netplan=true netplan/global/set-fail-mode=standalone netplan/mcast_snooping_enable=false"
                for entry in extids.strip('"').split(' '):
                    if entry.startswith('netplan/') and '=' in entry:
                        setting, val = entry.split('=', 1)
                        clear_setting(t, iface, setting, val)
