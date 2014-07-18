# Copyright (c) 2012 NTT DOCOMO, INC.
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

import contextlib
import os
import re
import socket
import stat
import time

from ironic.common import disk_partitioner
from ironic.common import exception
from ironic.common import utils
from ironic.openstack.common import excutils
from ironic.openstack.common import log as logging
from ironic.openstack.common import processutils


LOG = logging.getLogger(__name__)


# All functions are called from deploy() directly or indirectly.
# They are split for stub-out.

def discovery(portal_address, portal_port):
    """Do iSCSI discovery on portal."""
    utils.execute('iscsiadm',
                  '-m', 'discovery',
                  '-t', 'st',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  run_as_root=True,
                  check_exit_code=[0],
                  attempts=5,
                  delay_on_retry=True)


def login_iscsi(portal_address, portal_port, target_iqn):
    """Login to an iSCSI target."""
    utils.execute('iscsiadm',
                  '-m', 'node',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  '-T', target_iqn,
                  '--login',
                  run_as_root=True,
                  check_exit_code=[0],
                  attempts=5,
                  delay_on_retry=True)
    # Ensure the login complete
    time.sleep(3)


def logout_iscsi(portal_address, portal_port, target_iqn):
    """Logout from an iSCSI target."""
    utils.execute('iscsiadm',
                  '-m', 'node',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  '-T', target_iqn,
                  '--logout',
                  run_as_root=True,
                  check_exit_code=[0],
                  attempts=5,
                  delay_on_retry=True)


def delete_iscsi(portal_address, portal_port, target_iqn):
    """Delete the iSCSI target."""
    # Retry delete until it succeeds (exit code 0) or until there is
    # no longer a target to delete (exit code 21).
    utils.execute('iscsiadm',
                  '-m', 'node',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  '-T', target_iqn,
                  '-o', 'delete',
                  run_as_root=True,
                  check_exit_code=[0, 21],
                  attempts=5,
                  delay_on_retry=True)


def make_partitions(dev, root_mb, swap_mb, ephemeral_mb, commit=True):
    """Create partitions for root, swap and ephemeral on a disk device.

    :param root_mb: Size of the root partition in mebibytes (MiB).
    :param swap_mb: Size of the swap partition in mebibytes (MiB). If 0,
        no swap partition will be created.
    :param ephemeral_mb: Size of the ephemeral partition in mebibytes (MiB).
        If 0, no ephemeral partition will be created.
    :param commit: True/False. Default for this setting is True. If False
        partitions will not be written to disk.
    :returns: A dictionary containing the partition type as Key and partition
        path as Value for the partitions created by this method.

    """
    part_template = dev + '-part%d'
    part_dict = {}
    dp = disk_partitioner.DiskPartitioner(dev)
    if ephemeral_mb:
        part_num = dp.add_partition(ephemeral_mb)
        part_dict['ephemeral'] = part_template % part_num

    if swap_mb:
        part_num = dp.add_partition(swap_mb, fs_type='linux-swap')
        part_dict['swap'] = part_template % part_num

    # NOTE(lucasagomes): Make the root partition the last partition. This
    # enables tools like cloud-init's growroot utility to expand the root
    # partition until the end of the disk.
    part_num = dp.add_partition(root_mb)
    part_dict['root'] = part_template % part_num

    if commit:
        # write to the disk
        dp.commit()
    return part_dict


def is_block_device(dev):
    """Check whether a device is block or not."""
    s = os.stat(dev)
    return stat.S_ISBLK(s.st_mode)


def dd(src, dst):
    """Execute dd from src to dst."""
    utils.execute('dd',
                  'if=%s' % src,
                  'of=%s' % dst,
                  'bs=1M',
                  'oflag=direct',
                  run_as_root=True,
                  check_exit_code=[0])


def mkswap(dev, label='swap1'):
    """Execute mkswap on a device."""
    utils.mkfs('swap', dev, label)


def mkfs_ephemeral(dev, ephemeral_format, label="ephemeral0"):
    utils.mkfs(ephemeral_format, dev, label)


def block_uuid(dev):
    """Get UUID of a block device."""
    out, _ = utils.execute('blkid', '-s', 'UUID', '-o', 'value', dev,
                           run_as_root=True,
                           check_exit_code=[0])
    return out.strip()


def switch_pxe_config(path, root_uuid):
    """Switch a pxe config from deployment mode to service mode."""
    with open(path) as f:
        lines = f.readlines()
    root = 'UUID=%s' % root_uuid
    rre = re.compile(r'\{\{ ROOT \}\}')
    dre = re.compile('^default .*$')
    with open(path, 'w') as f:
        for line in lines:
            line = rre.sub(root, line)
            line = dre.sub('default boot', line)
            f.write(line)


def notify(address, port):
    """Notify a node that it becomes ready to reboot."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((address, port))
        s.send('done')
    finally:
        s.close()


def get_dev(address, port, iqn, lun):
    """Returns a device path for given parameters."""
    dev = "/dev/disk/by-path/ip-%s:%s-iscsi-%s-lun-%s" \
            % (address, port, iqn, lun)
    return dev


def get_image_mb(image_path):
    """Get size of an image in Megabyte."""
    mb = 1024 * 1024
    image_byte = os.path.getsize(image_path)
    # round up size to MB
    image_mb = int((image_byte + mb - 1) / mb)
    return image_mb


def get_dev_block_size(dev):
    """Get the device size in 512 byte sectors."""
    block_sz, cmderr = utils.execute('blockdev', '--getsz', dev,
                                     run_as_root=True, check_exit_code=[0])
    return int(block_sz)


def destroy_disk_metadata(dev, node_uuid):
    """Destroy metadata structures on node's disk.

       Ensure that node's disk appears to be blank without zeroing the entire
       drive. To do this we will zero:
       - the first 18KiB to clear MBR / GPT data
       - the last 18KiB to clear GPT and other metadata like: LVM, veritas,
         MDADM, DMRAID, ...
    """
    # NOTE(NobodyCam): This is needed to work around bug:
    # https://bugs.launchpad.net/ironic/+bug/1317647
    try:
        utils.execute('dd', 'if=/dev/zero', 'of=%s' % dev,
                      'bs=512', 'count=36', run_as_root=True,
                      check_exit_code=[0])
    except processutils.ProcessExecutionError as err:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Failed to erase beginning of disk for node "
                        "%(node)s. Command: %(command)s. Error: %(error)s."),
                      {'node': node_uuid,
                       'command': err.cmd,
                       'error': err.stderr})

    # now wipe the end of the disk.
    # get end of disk seek value
    try:
        block_sz = get_dev_block_size(dev)
    except processutils.ProcessExecutionError as err:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Failed to get disk block count for node %(node)s. "
                        "Command: %(command)s. Error: %(error)s."),
                      {'node': node_uuid,
                       'command': err.cmd,
                       'error': err.stderr})
    else:
        seek_value = block_sz - 36
        try:
            utils.execute('dd', 'if=/dev/zero', 'of=%s' % dev,
                          'bs=512', 'count=36', 'seek=%d' % seek_value,
                          run_as_root=True, check_exit_code=[0])
        except processutils.ProcessExecutionError as err:
            with excutils.save_and_reraise_exception():
                LOG.error(_("Failed to erase the end of the disk on node "
                            "%(node)s. Command: %(command)s. "
                            "Error: %(error)s."),
                          {'node': node_uuid,
                           'command': err.cmd,
                           'error': err.stderr})


def work_on_disk(dev, root_mb, swap_mb, ephemeral_mb, ephemeral_format,
                 image_path, node_uuid, preserve_ephemeral=False):
    """Create partitions and copy an image to the root partition.

    :param dev: Path for the device to work on.
    :param root_mb: Size of the root partition in megabytes.
    :param swap_mb: Size of the swap partition in megabytes.
    :param ephemeral_mb: Size of the ephemeral partition in megabytes. If 0,
        no ephemeral partition will be created.
    :param ephemeral_format: The type of file system to format the ephemeral
        partition.
    :param image_path: Path for the instance's disk image.
    :param node_uuid: node's uuid. Used for logging.
    :param preserve_ephemeral: If True, no filesystem is written to the
        ephemeral block device, preserving whatever content it had (if the
        partition table has not changed).

    """
    if not is_block_device(dev):
        raise exception.InstanceDeployFailure(_("Parent device '%s' not found")
                                              % dev)

    image_mb = get_image_mb(image_path)
    if image_mb > root_mb:
        root_mb = image_mb

    # the only way for preserve_ephemeral to be set to true is if we are
    # rebuilding an instance with --preserve_ephemeral.
    commit = not preserve_ephemeral
    # now if we are committing the changes to disk clean first.
    if commit:
        destroy_disk_metadata(dev, node_uuid)
    part_dict = make_partitions(dev, root_mb, swap_mb, ephemeral_mb,
                                commit=commit)

    ephemeral_part = part_dict.get('ephemeral')
    swap_part = part_dict.get('swap')
    root_part = part_dict.get('root')

    if not is_block_device(root_part):
        raise exception.InstanceDeployFailure(_("Root device '%s' not found")
                                              % root_part)
    if swap_part and not is_block_device(swap_part):
        raise exception.InstanceDeployFailure(_("Swap device '%s' not found")
                                              % swap_part)
    if ephemeral_part and not is_block_device(ephemeral_part):
        raise exception.InstanceDeployFailure(
                         _("Ephemeral device '%s' not found") % ephemeral_part)

    dd(image_path, root_part)

    if swap_part:
        mkswap(swap_part)

    if ephemeral_part and not preserve_ephemeral:
        mkfs_ephemeral(ephemeral_part, ephemeral_format)

    try:
        root_uuid = block_uuid(root_part)
    except processutils.ProcessExecutionError:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Failed to detect root device UUID."))
    return root_uuid


def write_to_disk(image_path, dev, node_uuid):
    """Write an image directly to the disk.

    :param image_path: Path for the instance's disk image.\
    :param dev: Path for the device to work on.

    """
    if not is_block_device(dev):
        raise exception.InstanceDeployFailure(_("Parent device '%s' not found")
                                              % dev)
    destroy_disk_metadata(dev, node_uuid)
    dd(image_path, dev)


@contextlib.contextmanager
def _iscsi_setup_and_handle_errors(address, port, iqn, lun):
    """Function that yields an iSCSI target device to work on.
    :param address: The iSCSI IP address.
    :param port: The iSCSI port number.
    :param iqn: The iSCSI qualified name.
    :param lun: The iSCSI logical unit number.

    """
    discovery(address, port)
    login_iscsi(address, port, iqn)
    dev = get_dev(address, port, iqn, lun)
    try:
        yield dev
    except processutils.ProcessExecutionError as err:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Deploy to address %s failed.") % address)
            LOG.error(_("Command: %s") % err.cmd)
            LOG.error(_("StdOut: %r") % err.stdout)
            LOG.error(_("StdErr: %r") % err.stderr)
    except exception.InstanceDeployFailure as e:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Deploy to address %s failed.") % address)
            LOG.error(e)
    finally:
        logout_iscsi(address, port, iqn)
        delete_iscsi(address, port, iqn)
    # Ensure the node started netcat on the port after POST the request.
    time.sleep(3)
    notify(address, 10000)


def deploy_partition_image(address, port, iqn, lun, image_path,
                           pxe_config_path, root_mb, swap_mb,
                           ephemeral_mb, ephemeral_format, node_uuid,
                           preserve_ephemeral=False):
    """Function to deploy partition images.

    :param address: The iSCSI IP address.
    :param port: The iSCSI port number.
    :param iqn: The iSCSI qualified name.
    :param lun: The iSCSI logical unit number.
    :param image_path: Path for the instance's disk image.
    :param pxe_config_path: Path for the instance PXE config file.
    :param root_mb: Size of the root partition in megabytes.
    :param swap_mb: Size of the swap partition in megabytes.
    :param ephemeral_mb: Size of the ephemeral partition in megabytes.
        If 0, no ephemeral partition will be created.
    :param ephemeral_format: The type of file system to format the
        ephemeral partition.
    :param node_uuid: node's uuid. Used for logging.        
    :param preserve_ephemeral: If True, no filesystem is written to the
        ephemeral block device, preserving whatever content it had (if the
        partition table has not changed).

    """
    with _iscsi_setup_and_handle_errors(address, port, iqn, lun) as dev:
        root_uuid = work_on_disk(dev, root_mb, swap_mb, ephemeral_mb,
                                 ephemeral_format, image_path, node_uuid,
                                 preserve_ephemeral)
        switch_pxe_config(pxe_config_path, root_uuid)


def deploy_disk_image(address, port, iqn, lun, image_path, node_uuid):
    """Function to deploy a disk image.

    :param address: The iSCSI IP address.
    :param port: The iSCSI port number.
    :param iqn: The iSCSI qualified name.
    :param lun: The iSCSI logical unit number.
    :param image_path: Path for the instance's disk image.

    """
    with _iscsi_setup_and_handle_errors(address, port, iqn, lun) as dev:
        write_to_disk(image_path, dev, node_uuid)
