# coding=utf-8

# Copyright 2013 Hewlett-Packard Development Company, L.P.
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

"""Test class for PXE driver."""

import fixtures
import mock
import os
import tempfile

from oslo.config import cfg

from ironic.common import exception
from ironic.common.glance_service import base_image_service
from ironic.common import image_service
from ironic.common import keystone
from ironic.common import neutron
from ironic.common import states
from ironic.common import tftp
from ironic.common import utils
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.db import api as dbapi
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules import pxe
from ironic.openstack.common import context
from ironic.openstack.common import fileutils
from ironic.openstack.common import jsonutils as json
from ironic.tests import base
from ironic.tests.conductor import utils as mgr_utils
from ironic.tests.db import base as db_base
from ironic.tests.db import utils as db_utils
from ironic.tests.objects import utils as obj_utils


CONF = cfg.CONF

INST_INFO_DICT = db_utils.get_test_pxe_instance_info()
DRV_INFO_DICT = db_utils.get_test_pxe_driver_info()


class PXEValidateParametersTestCase(base.TestCase):

    def setUp(self):
        super(PXEValidateParametersTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.dbapi = dbapi.get_instance()

    def test__parse_deploy_info(self):
        # make sure we get back the expected things
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_pxe',
                                          instance_info=INST_INFO_DICT,
                                          driver_info=DRV_INFO_DICT)
        info = pxe._parse_deploy_info(node)
        self.assertIsNotNone(info.get('deploy_ramdisk'))
        self.assertIsNotNone(info.get('deploy_kernel'))
        self.assertIsNotNone(info.get('image_source'))
        self.assertIsNotNone(info.get('root_gb'))
        self.assertEqual(0, info.get('ephemeral_gb'))

    def test__parse_driver_info_missing_deploy_kernel(self):
        # make sure error is raised when info is missing
        info = dict(DRV_INFO_DICT)
        del info['pxe_deploy_kernel']
        node = obj_utils.create_test_node(self.context, driver_info=info)
        self.assertRaises(exception.InvalidParameterValue,
                pxe._parse_driver_info,
                node)

    def test__parse_driver_info_missing_deploy_ramdisk(self):
        # make sure error is raised when info is missing
        info = dict(DRV_INFO_DICT)
        del info['pxe_deploy_ramdisk']
        node = obj_utils.create_test_node(self.context, driver_info=info)
        self.assertRaises(exception.InvalidParameterValue,
                pxe._parse_driver_info,
                node)

    def test__parse_driver_info_good(self):
        # make sure we get back the expected things
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_pxe',
                                          driver_info=DRV_INFO_DICT)
        info = pxe._parse_driver_info(node)
        self.assertIsNotNone(info.get('deploy_ramdisk'))
        self.assertIsNotNone(info.get('deploy_kernel'))

    def test__parse_instance_info_good(self):
        # make sure we get back the expected things
        node = obj_utils.create_test_node(self.context,
                                          driver='fake_pxe',
                                          instance_info=INST_INFO_DICT)
        info = pxe._parse_instance_info(node)
        self.assertIsNotNone(info.get('image_source'))
        self.assertIsNotNone(info.get('root_gb'))
        self.assertEqual(0, info.get('ephemeral_gb'))

    def test__parse_instance_info_missing_instance_source(self):
        # make sure error is raised when info is missing
        info = dict(INST_INFO_DICT)
        del info['image_source']
        node = obj_utils.create_test_node(self.context, instance_info=info)
        self.assertRaises(exception.InvalidParameterValue,
                pxe._parse_instance_info,
                node)

    def test__parse_instance_info_missing_root_gb(self):
        # make sure error is raised when info is missing
        info = dict(INST_INFO_DICT)
        del info['root_gb']
        node = obj_utils.create_test_node(self.context, instance_info=info)
        self.assertRaises(exception.InvalidParameterValue,
                pxe._parse_instance_info,
                node)

    def test__parse_instance_info_invalid_root_gb(self):
        info = dict(INST_INFO_DICT)
        info['root_gb'] = 'foobar'
        node = obj_utils.create_test_node(self.context, instance_info=info)
        self.assertRaises(exception.InvalidParameterValue,
                pxe._parse_instance_info,
                node)

    def test__parse_instance_info_valid_ephemeral_gb(self):
        ephemeral_gb = 10
        ephemeral_fmt = 'test-fmt'
        info = dict(INST_INFO_DICT)
        info['ephemeral_gb'] = ephemeral_gb
        info['ephemeral_format'] = ephemeral_fmt
        node = obj_utils.create_test_node(self.context, instance_info=info)
        data = pxe._parse_instance_info(node)
        self.assertEqual(ephemeral_gb, data.get('ephemeral_gb'))
        self.assertEqual(ephemeral_fmt, data.get('ephemeral_format'))

    def test__parse_instance_info_invalid_ephemeral_gb(self):
        info = dict(INST_INFO_DICT)
        info['ephemeral_gb'] = 'foobar'
        info['ephemeral_format'] = 'exttest'
        node = obj_utils.create_test_node(self.context, instance_info=info)
        self.assertRaises(exception.InvalidParameterValue,
                pxe._parse_instance_info,
                node)

    def test__parse_instance_info_valid_ephemeral_missing_format(self):
        ephemeral_gb = 10
        ephemeral_fmt = 'test-fmt'
        info = dict(INST_INFO_DICT)
        info['ephemeral_gb'] = ephemeral_gb
        info['ephemeral_format'] = None
        self.config(default_ephemeral_format=ephemeral_fmt, group='pxe')
        node = obj_utils.create_test_node(self.context, instance_info=info)
        instance_info = pxe._parse_instance_info(node)
        self.assertEqual(ephemeral_fmt, instance_info['ephemeral_format'])

    def test__parse_instance_info_valid_preserve_ephemeral_true(self):
        info = dict(INST_INFO_DICT)
        for _id, opt in enumerate(['true', 'TRUE', 'True', 't',
                                   'on', 'yes', 'y', '1']):
            info['preserve_ephemeral'] = opt
            node = obj_utils.create_test_node(self.context, id=_id,
                                              uuid=utils.generate_uuid(),
                                              instance_info=info)
            data = pxe._parse_instance_info(node)
            self.assertTrue(data.get('preserve_ephemeral'))

    def test__parse_instance_info_valid_preserve_ephemeral_false(self):
        info = dict(INST_INFO_DICT)
        for _id, opt in enumerate(['false', 'FALSE', 'False', 'f',
                                   'off', 'no', 'n', '0']):
            info['preserve_ephemeral'] = opt
            node = obj_utils.create_test_node(self.context, id=_id,
                                              uuid=utils.generate_uuid(),
                                              instance_info=info)
            data = pxe._parse_instance_info(node)
            self.assertFalse(data.get('preserve_ephemeral'))

    def test__parse_instance_info_invalid_preserve_ephemeral(self):
        info = dict(INST_INFO_DICT)
        info['preserve_ephemeral'] = 'foobar'
        node = obj_utils.create_test_node(self.context, instance_info=info)
        self.assertRaises(exception.InvalidParameterValue,
                pxe._parse_instance_info,
                node)


class PXEPrivateMethodsTestCase(db_base.DbTestCase):

    def setUp(self):
        super(PXEPrivateMethodsTestCase, self).setUp()
        n = {
              'driver': 'fake_pxe',
              'instance_info': INST_INFO_DICT,
              'driver_info': DRV_INFO_DICT,
        }
        mgr_utils.mock_the_extension_manager(driver="fake_pxe")
        self.dbapi = dbapi.get_instance()
        self.context = context.get_admin_context()
        self.node = obj_utils.create_test_node(self.context, **n)

    def _create_test_port(self, **kwargs):
        p = db_utils.get_test_port(**kwargs)
        return self.dbapi.create_port(p)

    @mock.patch.object(base_image_service.BaseImageService, '_show')
    def test__get_tftp_image_info(self, show_mock):
        properties = {'properties': {u'kernel_id': u'instance_kernel_uuid',
                     u'ramdisk_id': u'instance_ramdisk_uuid'}}

        expected_info = {'ramdisk':
                         ('instance_ramdisk_uuid',
                          os.path.join(CONF.tftp.tftp_root,
                                       self.node.uuid,
                                       'ramdisk')),
                         'kernel':
                         ('instance_kernel_uuid',
                          os.path.join(CONF.tftp.tftp_root,
                                       self.node.uuid,
                                       'kernel')),
                         'deploy_ramdisk':
                         ('deploy_ramdisk_uuid',
                           os.path.join(CONF.tftp.tftp_root,
                                        self.node.uuid,
                                        'deploy_ramdisk')),
                         'deploy_kernel':
                         ('deploy_kernel_uuid',
                          os.path.join(CONF.tftp.tftp_root,
                                       self.node.uuid,
                                       'deploy_kernel'))}
        show_mock.return_value = properties
        image_info = pxe._get_tftp_image_info(self.node, self.context)
        show_mock.assert_called_once_with('glance://image_uuid',
                                           method='get')
        self.assertEqual(expected_info, image_info)

        # test with saved info
        show_mock.reset_mock()
        image_info = pxe._get_tftp_image_info(self.node, self.context)
        self.assertEqual(expected_info, image_info)
        self.assertFalse(show_mock.called)
        self.assertEqual('instance_kernel_uuid',
                         self.node.instance_info.get('kernel'))
        self.assertEqual('instance_ramdisk_uuid',
                         self.node.instance_info.get('ramdisk'))

    @mock.patch.object(utils, 'random_alnum')
    @mock.patch.object(tftp, 'build_pxe_config')
    def test_build_pxe_config_options(self, build_pxe_mock, random_alnum_mock):
        self.config(pxe_append_params='test_param', group='pxe')
        # NOTE: right '/' should be removed from url string
        self.config(api_url='http://192.168.122.184:6385/', group='conductor')
        pxe_template = 'pxe_config_template'
        self.config(pxe_config_template=pxe_template, group='pxe')

        fake_key = '0123456789ABCDEFGHIJKLMNOPQRSTUV'
        random_alnum_mock.return_value = fake_key

        expected_options = {
            'deployment_key': '0123456789ABCDEFGHIJKLMNOPQRSTUV',
            'ari_path': u'/tftpboot/1be26c0b-03f2-4d2e-ae87-c02d7f33c123/'
                        u'ramdisk',
            'deployment_iscsi_iqn': u'iqn-1be26c0b-03f2-4d2e-ae87-c02d7f33'
                                    u'c123',
            'deployment_ari_path': u'/tftpboot/1be26c0b-03f2-4d2e-ae87-c02d7'
                                   u'f33c123/deploy_ramdisk',
            'pxe_append_params': 'test_param',
            'aki_path': u'/tftpboot/1be26c0b-03f2-4d2e-ae87-c02d7f33c123/'
                        u'kernel',
            'deployment_id': u'1be26c0b-03f2-4d2e-ae87-c02d7f33c123',
            'ironic_api_url': 'http://192.168.122.184:6385',
            'deployment_aki_path': u'/tftpboot/1be26c0b-03f2-4d2e-ae87-'
                                   u'c02d7f33c123/deploy_kernel'
        }
        image_info = {'deploy_kernel': ('deploy_kernel',
                                        os.path.join(CONF.tftp.tftp_root,
                                                     self.node.uuid,
                                                     'deploy_kernel')),
                      'deploy_ramdisk': ('deploy_ramdisk',
                                         os.path.join(CONF.tftp.tftp_root,
                                                      self.node.uuid,
                                                      'deploy_ramdisk')),
                      'kernel': ('kernel_id',
                                 os.path.join(CONF.tftp.tftp_root,
                                              self.node.uuid,
                                              'kernel')),
                      'ramdisk': ('ramdisk_id',
                                  os.path.join(CONF.tftp.tftp_root,
                                               self.node.uuid,
                                               'ramdisk'))
                      }
        options = pxe._build_pxe_config_options(self.node,
                                                image_info,
                                                self.context)
        self.assertEqual(expected_options, options)

        random_alnum_mock.assert_called_once_with(32)

        # test that deploy_key saved
        db_node = self.dbapi.get_node_by_uuid(self.node.uuid)
        db_key = db_node.instance_info.get('deploy_key')
        self.assertEqual(fake_key, db_key)

    def test__get_image_dir_path(self):
        self.assertEqual(os.path.join(CONF.pxe.images_path, self.node.uuid),
                         pxe._get_image_dir_path(self.node.uuid))

    def test__get_image_file_path(self):
        self.assertEqual(os.path.join(CONF.pxe.images_path,
                                      self.node.uuid,
                                      'disk'),
                         pxe._get_image_file_path(self.node.uuid))

    def test_get_token_file_path(self):
        node_uuid = self.node.uuid
        self.assertEqual('/tftpboot/token-' + node_uuid,
                         pxe._get_token_file_path(node_uuid))

    @mock.patch.object(pxe, '_fetch_images')
    def test__cache_tftp_images_master_path(self, mock_fetch_image):
        temp_dir = tempfile.mkdtemp()
        self.config(tftp_root=temp_dir, group='tftp')
        self.config(tftp_master_path=os.path.join(temp_dir,
                                                  'tftp_master_path'),
                    group='pxe')
        image_path = os.path.join(temp_dir, self.node.uuid,
                                  'deploy_kernel')
        image_info = {'deploy_kernel': ('deploy_kernel', image_path)}
        fileutils.ensure_tree(CONF.pxe.tftp_master_path)

        pxe._cache_tftp_images(None, self.node, image_info)

        mock_fetch_image.assert_called_once_with(None,
                                                 mock.ANY,
                                                 [('deploy_kernel',
                                                   image_path)])

    @mock.patch.object(pxe, '_fetch_images')
    def test__cache_instance_images_master_path(self, mock_fetch_image):
        temp_dir = tempfile.mkdtemp()
        self.config(images_path=temp_dir, group='pxe')
        self.config(instance_master_path=os.path.join(temp_dir,
                                                      'instance_master_path'),
                    group='pxe')
        fileutils.ensure_tree(CONF.pxe.instance_master_path)

        (uuid, image_path) = pxe._cache_instance_image(None,
                                                       self.node)
        mock_fetch_image.assert_called_once_with(None,
                                                 mock.ANY,
                                                 [(uuid, image_path)])
        self.assertEqual('glance://image_uuid', uuid)
        self.assertEqual(os.path.join(temp_dir,
                                      self.node.uuid,
                                      'disk'),
                         image_path)


@mock.patch.object(pxe, 'TFTPImageCache')
@mock.patch.object(pxe, 'InstanceImageCache')
@mock.patch.object(os, 'statvfs')
@mock.patch.object(image_service, 'Service')
class PXEPrivateFetchImagesTestCase(db_base.DbTestCase):

    def test_no_clean_up(self, mock_image_service, mock_statvfs,
                         mock_instance_cache, mock_tftp_cache):
        # Enough space found - no clean up
        mock_show = mock_image_service.return_value.show
        mock_show.return_value = dict(size=42)
        mock_statvfs.return_value = mock.Mock(f_frsize=1, f_bavail=1024)

        cache = mock.Mock(master_dir='master_dir')
        pxe._fetch_images(None, cache, [('uuid', 'path')])

        mock_show.assert_called_once_with('uuid')
        mock_statvfs.assert_called_once_with('master_dir')
        cache.fetch_image.assert_called_once_with('uuid', 'path', ctx=None)
        self.assertFalse(mock_instance_cache.return_value.clean_up.called)
        self.assertFalse(mock_tftp_cache.return_value.clean_up.called)

    @mock.patch.object(os, 'stat')
    def test_one_clean_up(self, mock_stat, mock_image_service, mock_statvfs,
                          mock_instance_cache, mock_tftp_cache):
        # Not enough space, instance cache clean up is enough
        mock_stat.return_value.st_dev = 1
        mock_show = mock_image_service.return_value.show
        mock_show.return_value = dict(size=42)
        mock_statvfs.side_effect = [
            mock.Mock(f_frsize=1, f_bavail=1),
            mock.Mock(f_frsize=1, f_bavail=1024)
        ]

        cache = mock.Mock(master_dir='master_dir')
        pxe._fetch_images(None, cache, [('uuid', 'path')])

        mock_show.assert_called_once_with('uuid')
        mock_statvfs.assert_called_with('master_dir')
        self.assertEqual(2, mock_statvfs.call_count)
        cache.fetch_image.assert_called_once_with('uuid', 'path', ctx=None)
        mock_instance_cache.return_value.clean_up.assert_called_once_with(
            amount=(42 * 2 - 1))
        self.assertFalse(mock_tftp_cache.return_value.clean_up.called)
        self.assertEqual(3, mock_stat.call_count)

    @mock.patch.object(os, 'stat')
    def test_clean_up_another_fs(self, mock_stat, mock_image_service,
                                 mock_statvfs, mock_instance_cache,
                                 mock_tftp_cache):
        # Not enough space, instance cache on another partition
        mock_stat.side_effect = [mock.Mock(st_dev=1),
                                 mock.Mock(st_dev=2),
                                 mock.Mock(st_dev=1)]
        mock_show = mock_image_service.return_value.show
        mock_show.return_value = dict(size=42)
        mock_statvfs.side_effect = [
            mock.Mock(f_frsize=1, f_bavail=1),
            mock.Mock(f_frsize=1, f_bavail=1024)
        ]

        cache = mock.Mock(master_dir='master_dir')
        pxe._fetch_images(None, cache, [('uuid', 'path')])

        mock_show.assert_called_once_with('uuid')
        mock_statvfs.assert_called_with('master_dir')
        self.assertEqual(2, mock_statvfs.call_count)
        cache.fetch_image.assert_called_once_with('uuid', 'path', ctx=None)
        mock_tftp_cache.return_value.clean_up.assert_called_once_with(
            amount=(42 * 2 - 1))
        self.assertFalse(mock_instance_cache.return_value.clean_up.called)
        self.assertEqual(3, mock_stat.call_count)

    @mock.patch.object(os, 'stat')
    def test_both_clean_up(self, mock_stat, mock_image_service, mock_statvfs,
                           mock_instance_cache, mock_tftp_cache):
        # Not enough space, clean up of both caches required
        mock_stat.return_value.st_dev = 1
        mock_show = mock_image_service.return_value.show
        mock_show.return_value = dict(size=42)
        mock_statvfs.side_effect = [
            mock.Mock(f_frsize=1, f_bavail=1),
            mock.Mock(f_frsize=1, f_bavail=2),
            mock.Mock(f_frsize=1, f_bavail=1024)
        ]

        cache = mock.Mock(master_dir='master_dir')
        pxe._fetch_images(None, cache, [('uuid', 'path')])

        mock_show.assert_called_once_with('uuid')
        mock_statvfs.assert_called_with('master_dir')
        self.assertEqual(3, mock_statvfs.call_count)
        cache.fetch_image.assert_called_once_with('uuid', 'path', ctx=None)
        mock_instance_cache.return_value.clean_up.assert_called_once_with(
            amount=(42 * 2 - 1))
        mock_tftp_cache.return_value.clean_up.assert_called_once_with(
            amount=(42 * 2 - 2))
        self.assertEqual(3, mock_stat.call_count)

    @mock.patch.object(os, 'stat')
    def test_clean_up_fail(self, mock_stat, mock_image_service, mock_statvfs,
                           mock_instance_cache, mock_tftp_cache):
        # Not enough space even after cleaning both caches - failure
        mock_stat.return_value.st_dev = 1
        mock_show = mock_image_service.return_value.show
        mock_show.return_value = dict(size=42)
        mock_statvfs.return_value = mock.Mock(f_frsize=1, f_bavail=1)

        cache = mock.Mock(master_dir='master_dir')
        self.assertRaises(exception.InstanceDeployFailure, pxe._fetch_images,
                          None, cache, [('uuid', 'path')])

        mock_show.assert_called_once_with('uuid')
        mock_statvfs.assert_called_with('master_dir')
        self.assertEqual(3, mock_statvfs.call_count)
        self.assertFalse(cache.return_value.fetch_image.called)
        mock_instance_cache.return_value.clean_up.assert_called_once_with(
            amount=(42 * 2 - 1))
        mock_tftp_cache.return_value.clean_up.assert_called_once_with(
            amount=(42 * 2 - 1))
        self.assertEqual(3, mock_stat.call_count)


class PXEDriverTestCase(db_base.DbTestCase):

    def setUp(self):
        super(PXEDriverTestCase, self).setUp()
        self.context = context.get_admin_context()
        self.context.auth_token = '4562138218392831'
        self.temp_dir = tempfile.mkdtemp()
        self.config(tftp_root=self.temp_dir, group='tftp')
        self.temp_dir = tempfile.mkdtemp()
        self.config(images_path=self.temp_dir, group='pxe')
        mgr_utils.mock_the_extension_manager(driver="fake_pxe")
        instance_info = INST_INFO_DICT
        instance_info['deploy_key'] = 'fake-56789'
        self.node = obj_utils.create_test_node(self.context,
                                               driver='fake_pxe',
                                               instance_info=instance_info,
                                               driver_info=DRV_INFO_DICT)
        self.dbapi = dbapi.get_instance()
        self.port = self.dbapi.create_port(db_utils.get_test_port(
                                                         node_id=self.node.id))
        self.config(group='conductor', api_url='http://127.0.0.1:1234/')

    def _create_token_file(self):
        token_path = pxe._get_token_file_path(self.node.uuid)
        open(token_path, 'w').close()
        return token_path

    @mock.patch.object(base_image_service.BaseImageService, '_show')
    def test_validate_good(self, mock_glance):
        mock_glance.return_value = {'properties': {'kernel_id': 'fake-kernel',
                                                   'ramdisk_id': 'fake-initr'}}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.deploy.validate(task)

    def test_validate_fail(self):
        info = dict(INST_INFO_DICT)
        del info['image_source']
        self.node.instance_info = json.dumps(info)
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.node['instance_info'] = json.dumps(info)
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.deploy.validate, task)

    def test_validate_fail_no_port(self):
        new_node = obj_utils.create_test_node(
                self.context,
                id=321, uuid='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                driver='fake_pxe', instance_info=INST_INFO_DICT,
                driver_info=DRV_INFO_DICT)
        with task_manager.acquire(self.context, new_node.uuid,
                                  shared=True) as task:
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.deploy.validate, task)

    @mock.patch.object(base_image_service.BaseImageService, '_show')
    @mock.patch.object(keystone, 'get_service_url')
    def test_validate_good_api_url_from_config_file(self, mock_ks,
                                                    mock_glance):
        mock_glance.return_value = {'properties': {'kernel_id': 'fake-kernel',
                                                   'ramdisk_id': 'fake-initr'}}
        # not present in the keystone catalog
        mock_ks.side_effect = exception.CatalogFailure

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.deploy.validate(task)
            self.assertFalse(mock_ks.called)

    @mock.patch.object(base_image_service.BaseImageService, '_show')
    @mock.patch.object(keystone, 'get_service_url')
    def test_validate_good_api_url_from_keystone(self, mock_ks, mock_glance):
        mock_glance.return_value = {'properties': {'kernel_id': 'fake-kernel',
                                                   'ramdisk_id': 'fake-initr'}}
        # present in the keystone catalog
        mock_ks.return_value = 'http://127.0.0.1:1234'
        # not present in the config file
        self.config(group='conductor', api_url=None)

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.deploy.validate(task)
            mock_ks.assert_called_once_with()

    @mock.patch.object(keystone, 'get_service_url')
    def test_validate_fail_no_api_url(self, mock_ks):
        # not present in the keystone catalog
        mock_ks.side_effect = exception.CatalogFailure
        # not present in the config file
        self.config(group='conductor', api_url=None)

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.deploy.validate, task)
            mock_ks.assert_called_once_with()

    @mock.patch.object(base_image_service.BaseImageService, '_show')
    def test_validate_fail_no_image_kernel_ramdisk_props(self, mock_glance):
        mock_glance.return_value = {'properties': {}}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.deploy.validate,
                              task)

    @mock.patch.object(base_image_service.BaseImageService, '_show')
    def test_validate_fail_glance_image_doesnt_exists(self, mock_glance):
        mock_glance.side_effect = exception.ImageNotFound('not found')
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.deploy.validate, task)

    @mock.patch.object(base_image_service.BaseImageService, '_show')
    def test_validate_fail_glance_conn_problem(self, mock_glance):
        exceptions = (exception.GlanceConnectionFailed('connection fail'),
                      exception.ImageNotAuthorized('not authorized'),
                      exception.Invalid('invalid'))
        mock_glance.side_effect = exceptions
        for exc in exceptions:
            with task_manager.acquire(self.context, self.node.uuid,
                                      shared=True) as task:
                self.assertRaises(exception.InvalidParameterValue,
                                  task.driver.deploy.validate, task)

    def test_vendor_passthru_validate_good(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.vendor.validate(task, method='pass_deploy_info',
                                        address='123456', iqn='aaa-bbb',
                                        key='fake-56789')

    def test_vendor_passthru_validate_fail(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.vendor.validate,
                              task, method='pass_deploy_info',
                              key='fake-56789')

    def test_vendor_passthru_validate_key_notmatch(self):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            self.assertRaises(exception.InvalidParameterValue,
                              task.driver.vendor.validate,
                              task, method='pass_deploy_info',
                              address='123456', iqn='aaa-bbb',
                              key='fake-12345')

    @mock.patch.object(pxe, '_get_tftp_image_info')
    @mock.patch.object(pxe, '_cache_tftp_images')
    @mock.patch.object(pxe, '_build_pxe_config_options')
    @mock.patch.object(tftp, 'create_pxe_config')
    def test_prepare(self, mock_pxe_config,
                     mock_build_pxe, mock_cache_tftp_images,
                     mock_tftp_img_info):
        mock_build_pxe.return_value = None
        mock_tftp_img_info.return_value = None
        mock_pxe_config.return_value = None
        mock_cache_tftp_images.return_value = None
        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.deploy.prepare(task)
            mock_tftp_img_info.assert_called_once_with(task.node,
                                                       self.context)
            mock_pxe_config.assert_called_once_with(
                task, None, CONF.pxe.pxe_config_template)
            mock_cache_tftp_images.assert_called_once_with(self.context,
                                                           task.node, None)

    @mock.patch.object(deploy_utils, 'get_image_mb')
    @mock.patch.object(pxe, '_get_image_file_path')
    @mock.patch.object(pxe, '_cache_instance_image')
    @mock.patch.object(neutron, 'update_neutron')
    @mock.patch.object(manager_utils, 'node_power_action')
    @mock.patch.object(manager_utils, 'node_set_boot_device')
    def test_deploy(self, mock_node_set_boot, mock_node_power_action,
                    mock_update_neutron, mock_cache_instance_image,
                    mock_get_image_file_path, mock_get_image_mb):
        fake_img_path = '/test/path/test.img'
        mock_get_image_file_path.return_value = fake_img_path
        mock_get_image_mb.return_value = 1

        with task_manager.acquire(self.context,
            self.node.uuid, shared=False) as task:
            state = task.driver.deploy.deploy(task)
            self.assertEqual(state, states.DEPLOYWAIT)
            mock_cache_instance_image.assert_called_once_with(
                self.context, task.node)
            mock_get_image_file_path.assert_called_once_with(task.node.uuid)
            mock_get_image_mb.assert_called_once_with(fake_img_path)
            mock_update_neutron.assert_called_once_with(
                task, CONF.pxe.pxe_bootfile_name)
            mock_node_set_boot.assert_called_once_with(task, 'pxe',
                                                       persistent=True)
            mock_node_power_action.assert_called_once_with(task, states.REBOOT)

            # ensure token file created
            t_path = pxe._get_token_file_path(self.node.uuid)
            token = open(t_path, 'r').read()
            self.assertEqual(self.context.auth_token, token)

    @mock.patch.object(deploy_utils, 'get_image_mb')
    @mock.patch.object(pxe, '_get_image_file_path')
    @mock.patch.object(pxe, '_cache_instance_image')
    def test_deploy_image_too_large(self, mock_cache_instance_image,
                                    mock_get_image_file_path,
                                    mock_get_image_mb):
        fake_img_path = '/test/path/test.img'
        mock_get_image_file_path.return_value = fake_img_path
        mock_get_image_mb.return_value = 999999

        with task_manager.acquire(self.context,
                                  self.node.uuid, shared=False) as task:
            self.assertRaises(exception.InstanceDeployFailure,
                task.driver.deploy.deploy, task)
            mock_cache_instance_image.assert_called_once_with(
                self.context, task.node)
            mock_get_image_file_path.assert_called_once_with(task.node.uuid)
            mock_get_image_mb.assert_called_once_with(fake_img_path)

    @mock.patch.object(manager_utils, 'node_power_action')
    def test_tear_down(self, node_power_mock):
        with task_manager.acquire(self.context,
                                  self.node.uuid) as task:
            state = task.driver.deploy.tear_down(task)
            self.assertEqual(states.DELETED, state)
            node_power_mock.assert_called_once_with(task, states.POWER_OFF)

    @mock.patch.object(neutron, 'update_neutron')
    def test_take_over(self, update_neutron_mock):
        with task_manager.acquire(
                self.context, self.node.uuid, shared=True) as task:
            task.driver.deploy.take_over(task)
            update_neutron_mock.assert_called_once_with(
                task, CONF.pxe.pxe_bootfile_name)

    @mock.patch.object(pxe, 'InstanceImageCache')
    def test_continue_deploy_good(self, mock_image_cache):
        token_path = self._create_token_file()
        self.node.power_state = states.POWER_ON
        self.node.provision_state = states.DEPLOYWAIT
        self.node.save()

        def fake_deploy(**kwargs):
            pass

        self.useFixture(fixtures.MonkeyPatch(
                'ironic.drivers.modules.deploy_utils.deploy',
                fake_deploy))

        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.vendor.vendor_passthru(
                    task, method='pass_deploy_info', address='123456',
                    iqn='aaa-bbb', key='fake-56789')
        self.node.refresh(self.context)
        self.assertEqual(states.ACTIVE, self.node.provision_state)
        self.assertEqual(states.POWER_ON, self.node.power_state)
        self.assertIsNone(self.node.last_error)
        self.assertFalse(os.path.exists(token_path))
        mock_image_cache.assert_called_once_with()
        mock_image_cache.return_value.clean_up.assert_called_once_with()

    @mock.patch.object(pxe, 'InstanceImageCache')
    def test_continue_deploy_fail(self, mock_image_cache):
        token_path = self._create_token_file()
        self.node.power_state = states.POWER_ON
        self.node.provision_state = states.DEPLOYWAIT
        self.node.save()

        def fake_deploy(**kwargs):
            raise exception.InstanceDeployFailure("test deploy error")

        self.useFixture(fixtures.MonkeyPatch(
                'ironic.drivers.modules.deploy_utils.deploy',
                fake_deploy))

        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.vendor.vendor_passthru(
                    task, method='pass_deploy_info', address='123456',
                    iqn='aaa-bbb', key='fake-56789')
        self.node.refresh(self.context)
        self.assertEqual(states.DEPLOYFAIL, self.node.provision_state)
        self.assertEqual(states.POWER_OFF, self.node.power_state)
        self.assertIsNotNone(self.node.last_error)
        self.assertFalse(os.path.exists(token_path))
        mock_image_cache.assert_called_once_with()
        mock_image_cache.return_value.clean_up.assert_called_once_with()

    @mock.patch.object(pxe, 'InstanceImageCache')
    def test_continue_deploy_ramdisk_fails(self, mock_image_cache):
        token_path = self._create_token_file()
        self.node.power_state = states.POWER_ON
        self.node.provision_state = states.DEPLOYWAIT
        self.node.save()

        def fake_deploy(**kwargs):
            pass

        self.useFixture(fixtures.MonkeyPatch(
                'ironic.drivers.modules.deploy_utils.deploy',
                fake_deploy))

        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.vendor.vendor_passthru(
                    task, method='pass_deploy_info', address='123456',
                    iqn='aaa-bbb', key='fake-56789',
                    error='test ramdisk error')
        self.node.refresh(self.context)
        self.assertEqual(states.DEPLOYFAIL, self.node.provision_state)
        self.assertEqual(states.POWER_OFF, self.node.power_state)
        self.assertIsNotNone(self.node.last_error)
        self.assertFalse(os.path.exists(token_path))
        mock_image_cache.assert_called_once_with()
        mock_image_cache.return_value.clean_up.assert_called_once_with()

    def test_continue_deploy_invalid(self):
        self.node.power_state = states.POWER_ON
        self.node.provision_state = 'FAKE'
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid) as task:
            task.driver.vendor.vendor_passthru(
                    task, method='pass_deploy_info', address='123456',
                    iqn='aaa-bbb', key='fake-56789',
                    error='test ramdisk error')
        self.node.refresh(self.context)
        self.assertEqual('FAKE', self.node.provision_state)
        self.assertEqual(states.POWER_ON, self.node.power_state)

    def test_lock_elevated(self):
        with task_manager.acquire(self.context, self.node.uuid) as task:
            with mock.patch.object(task.driver.vendor, '_continue_deploy') \
                    as _continue_deploy_mock:
                task.driver.vendor.vendor_passthru(task,
                    method='pass_deploy_info', address='123456', iqn='aaa-bbb',
                    key='fake-56789')
                # lock elevated w/o exception
                self.assertEqual(1, _continue_deploy_mock.call_count,
                            "_continue_deploy was not called once.")

    @mock.patch.object(pxe, '_get_tftp_image_info')
    def clean_up_config(self, get_tftp_image_info_mock, master=None):
        temp_dir = tempfile.mkdtemp()
        self.config(tftp_root=temp_dir, group='tftp')
        tftp_master_dir = os.path.join(CONF.tftp.tftp_root,
                                       'tftp_master')
        self.config(tftp_master_path=tftp_master_dir, group='pxe')
        os.makedirs(tftp_master_dir)

        instance_master_dir = os.path.join(CONF.pxe.images_path,
                                           'instance_master')
        self.config(instance_master_path=instance_master_dir,
                    group='pxe')
        os.makedirs(instance_master_dir)

        ports = []
        ports.append(
            self.dbapi.create_port(
                db_utils.get_test_port(
                    id=6,
                    address='aa:bb:cc',
                    uuid='bb43dc0b-03f2-4d2e-ae87-c02d7f33cc53',
                    node_id='123')))

        d_kernel_path = os.path.join(CONF.tftp.tftp_root,
                                     self.node.uuid, 'deploy_kernel')
        image_info = {'deploy_kernel': ('deploy_kernel_uuid', d_kernel_path)}

        get_tftp_image_info_mock.return_value = image_info

        pxecfg_dir = os.path.join(CONF.tftp.tftp_root, 'pxelinux.cfg')
        os.makedirs(pxecfg_dir)

        instance_dir = os.path.join(CONF.tftp.tftp_root,
                                    self.node.uuid)
        image_dir = os.path.join(CONF.pxe.images_path, self.node.uuid)
        os.makedirs(instance_dir)
        os.makedirs(image_dir)
        config_path = os.path.join(instance_dir, 'config')
        deploy_kernel_path = os.path.join(instance_dir, 'deploy_kernel')
        pxe_mac_path = os.path.join(pxecfg_dir, '01-aa-bb-cc')
        image_path = os.path.join(image_dir, 'disk')
        open(config_path, 'w').close()
        os.link(config_path, pxe_mac_path)
        if master:
            master_deploy_kernel_path = os.path.join(tftp_master_dir,
                                                     'deploy_kernel_uuid')
            master_instance_path = os.path.join(instance_master_dir,
                                                'image_uuid')
            open(master_deploy_kernel_path, 'w').close()
            open(master_instance_path, 'w').close()

            os.link(master_deploy_kernel_path, deploy_kernel_path)
            os.link(master_instance_path, image_path)
            if master == 'in_use':
                deploy_kernel_link = os.path.join(CONF.tftp.tftp_root,
                                                  'deploy_kernel_link')
                image_link = os.path.join(CONF.pxe.images_path,
                                          'image_link')
                os.link(master_deploy_kernel_path, deploy_kernel_link)
                os.link(master_instance_path, image_link)

        else:
            open(deploy_kernel_path, 'w').close()
            open(image_path, 'w').close()
        token_path = self._create_token_file()
        self.config(image_cache_size=0, group='pxe')

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            task.driver.deploy.clean_up(task)
            get_tftp_image_info_mock.called_once_with(task.node)
        assert_false_path = [config_path, deploy_kernel_path, image_path,
                             pxe_mac_path, image_dir, instance_dir,
                             token_path]
        for path in assert_false_path:
            self.assertFalse(os.path.exists(path))

    def test_clean_up_no_master_images(self):
        self.clean_up_config(master=None)

    def test_clean_up_master_images_in_use(self):
        # NOTE(dtantsur): ensure agressive caching
        self.config(image_cache_size=1, group='pxe')
        self.config(image_cache_ttl=0, group='pxe')

        self.clean_up_config(master='in_use')

        master_d_kernel_path = os.path.join(CONF.pxe.tftp_master_path,
                                            'deploy_kernel_uuid')
        master_instance_path = os.path.join(CONF.pxe.instance_master_path,
                                             'image_uuid')

        self.assertTrue(os.path.exists(master_d_kernel_path))
        self.assertTrue(os.path.exists(master_instance_path))
