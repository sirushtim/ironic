# Copyright 2014 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""This module detects whether third-party libraries, utilized by third-party
drivers, are present on the system. If they are not, it mocks them and tinkers
with sys.modules so that the drivers can be loaded by unit tests, and the unit
tests can continue to test the functionality of those drivers without the
respective external libraries' actually being present.

Any external library required by a third-party driver should be mocked here.
Current list of mocked libraries:
    seamicroclient
    ipminative
"""

import sys

import mock

from ironic.drivers.modules import ipmitool
from ironic.openstack.common import importutils


# attempt to load the external 'seamicroclient' library, which is
# required by the optional drivers.modules.seamicro module
seamicroclient = importutils.try_import("seamicroclient")
if not seamicroclient:
    smc = mock.Mock()
    smc.client = mock.Mock()
    smc.exceptions = mock.Mock()
    smc.exceptions.ClientException = Exception
    sys.modules['seamicroclient'] = smc
    sys.modules['seamicroclient.client'] = smc.client
    sys.modules['seamicroclient.exceptions'] = smc.exceptions

# if anything has loaded the seamicro driver yet, reload it now that
# the external library has been mocked
if 'ironic.drivers.modules.seamicro' in sys.modules:
    reload(sys.modules['ironic.drivers.modules.seamicro'])

# IPMITool driver checks the system for presense of 'ipmitool' binary during
# __init__. We bypass that check in order to run the unit tests, which do not
# depend on 'ipmitool' being on the system.
ipmitool.TIMING_SUPPORT = False

pyghmi = importutils.try_import("pyghmi")
if not pyghmi:
    p = mock.Mock()
    p.exceptions = mock.Mock()
    p.exceptions.IpmiException = Exception
    p.ipmi = mock.Mock()
    p.ipmi.command = mock.Mock()
    p.ipmi.command.Command = mock.Mock()
    sys.modules['pyghmi'] = p
    sys.modules['pyghmi.exceptions'] = p.exceptions
    sys.modules['pyghmi.ipmi'] = p.ipmi
    sys.modules['pyghmi.ipmi.command'] = p.ipmi.command
    # FIXME(deva): the next line is a hack, because several unit tests
    #              actually depend on this particular string being present
    #              in pyghmi.ipmi.command.boot_devices
    p.ipmi.command.boot_devices = {'pxe': 4}

if 'ironic.drivers.modules.ipminative' in sys.modules:
    reload(sys.modules['ironic.drivers.modules.ipminative'])
