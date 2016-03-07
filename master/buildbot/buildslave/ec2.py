# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Portions Copyright Buildbot Team Members

from __future__ import with_statement
# Portions Copyright Canonical Ltd. 2009

"""A LatentSlave that uses EC2 to instantiate the slaves on demand.

Tested with Python boto 1.5c
"""

import os
import re
import time

import boto
import boto.ec2
import boto.exception

from boto.ec2.blockdevicemapping import BlockDeviceType, BlockDeviceMapping

from twisted.internet import defer
from twisted.internet import threads
from twisted.python import log

from buildbot import interfaces
from buildbot.buildslave.base import AbstractLatentBuildSlave

PENDING = 'pending'
RUNNING = 'running'
SHUTTINGDOWN = 'shutting-down'
TERMINATED = 'terminated'
SPOT_REQUEST_PENDING_STATES = ['pending-evaluation', 'pending-fulfillment']
FULFILLED = 'fulfilled'
PRICE_TOO_LOW = 'price-too-low'

VOLUME_CREATING = 'creating'
VOLUME_AVAILABLE = 'available'
VOLUME_ISUSE = 'in-use'
VOLUME_DELETING = 'deleting'
VOLUME_DELETED = 'deleted'
VOLUME_ERROR = 'error'

class EC2LatentBuildSlave(AbstractLatentBuildSlave):

    instance = image = None
    _poll_resolution = 5  # hook point for tests
    _poll_retry = 10

    def __init__(self, name, password, instance_type, ami=None,
                 valid_ami_owners=None, valid_ami_location_regex=None,
                 elastic_ip=None, identifier=None, secret_identifier=None,
                 aws_id_file_path=None, user_data=None, region=None,
                 keypair_name=None,
                 security_name=None,
                 subnet_id=None, security_group_ids=None,
                 max_builds=None, notify_on_missing=[], missing_timeout=60 * 20,
                 build_wait_timeout=60 * 10, properties={}, locks=None,
                 spot_instance=False, max_spot_price=1.6, volumes=[],
                 placement=None, price_multiplier=1.2, tags={},
                 delete_vol_term=True, create_volumes=[],block_device_map=None):

        AbstractLatentBuildSlave.__init__(
            self, name, password, max_builds, notify_on_missing,
            missing_timeout, build_wait_timeout, properties, locks)
        if security_name and subnet_id:
            raise ValueError(
                'security_name (EC2 classic security groups) is not supported '
                'in a VPC.  Use security_group_ids instead.')
        if not ((ami is not None) ^
                (valid_ami_owners is not None or
                 valid_ami_location_regex is not None)):
            raise ValueError(
                'You must provide either a specific ami, or one or both of '
                'valid_ami_location_regex and valid_ami_owners')
        self.ami = ami
        if valid_ami_owners is not None:
            if isinstance(valid_ami_owners, (int, long)):
                valid_ami_owners = (valid_ami_owners,)
            else:
                for element in valid_ami_owners:
                    if not isinstance(element, (int, long)):
                        raise ValueError(
                            'valid_ami_owners should be int or iterable '
                            'of ints', element)
        if valid_ami_location_regex is not None:
            if not isinstance(valid_ami_location_regex, basestring):
                raise ValueError(
                    'valid_ami_location_regex should be a string')
            else:
                # verify that regex will compile
                re.compile(valid_ami_location_regex)
        if keypair_name is None:
            keypair_name = 'latent_buildbot_slave'
            log.msg('Using default keypair name, since none is set')
        if security_name is None and not subnet_id:
            security_name = 'latent_buildbot_slave'
            log.msg('Using default keypair name, since none is set')
        self.valid_ami_owners = valid_ami_owners
        self.valid_ami_location_regex = valid_ami_location_regex
        self.instance_type = instance_type
        self.keypair_name = keypair_name
        self.security_name = security_name
        self.user_data = user_data
        self.spot_instance = spot_instance
        self.max_spot_price = max_spot_price
        self.volumes = volumes
        self.price_multiplier = price_multiplier
        self.delete_vol_term = delete_vol_term
        self.create_volumes = create_volumes

        if None not in [placement, region]:
            self.placement = '%s%s' % (region, placement)
        else:
            self.placement = None
        if identifier is None:
            assert secret_identifier is None, (
                'supply both or neither of identifier, secret_identifier')
            if aws_id_file_path is None:
                home = os.environ['HOME']
                default_path = os.path.join(home, '.ec2', 'aws_id')
                if os.path.exists(default_path):
                    aws_id_file_path = default_path
            if aws_id_file_path:
                log.msg('WARNING: EC2LatentBuildSlave is using deprecated '
                        'aws_id file')
                with open(aws_id_file_path, 'r') as aws_file:
                    identifier = aws_file.readline().strip()
                    secret_identifier = aws_file.readline().strip()
        else:
            assert aws_id_file_path is None, \
                'if you supply the identifier and secret_identifier, ' \
                'do not specify the aws_id_file_path'
            assert secret_identifier is not None, \
                'supply both or neither of identifier, secret_identifier'

        region_found = None

        # Make the EC2 connection.
        if region is not None:
            for r in boto.ec2.regions(aws_access_key_id=identifier,
                                      aws_secret_access_key=secret_identifier):

                if r.name == region:
                    region_found = r

            if region_found is not None:
                self.conn = boto.ec2.connect_to_region(region,
                                                       aws_access_key_id=identifier,
                                                       aws_secret_access_key=secret_identifier)
            else:
                raise ValueError(
                    'The specified region does not exist: {0}'.format(region))

        else:
            self.conn = boto.connect_ec2(identifier, secret_identifier)

        # Make a keypair
        #
        # We currently discard the keypair data because we don't need it.
        # If we do need it in the future, we will always recreate the keypairs
        # because there is no way to
        # programmatically retrieve the private key component, unless we
        # generate it and store it on the filesystem, which is an unnecessary
        # usage requirement.
        try:
            key_pair = self.conn.get_all_key_pairs(keypair_name)[0]
            assert key_pair
            # key_pair.delete() # would be used to recreate
        except boto.exception.EC2ResponseError, e:
            if 'InvalidKeyPair.NotFound' not in e.body:
                if 'AuthFailure' in e.body:
                    print ('POSSIBLE CAUSES OF ERROR:\n'
                           '  Did you supply your AWS credentials?\n'
                           '  Did you sign up for EC2?\n'
                           '  Did you put a credit card number in your AWS '
                           'account?\n'
                           'Please doublecheck before reporting a problem.\n')
                raise
            # make one; we would always do this, and stash the result, if we
            # needed the key (for instance, to SSH to the box).  We'd then
            # use paramiko to use the key to connect.
            self.conn.create_key_pair(keypair_name)

        # create security group
        if security_name:
            try:
                group = self.conn.get_all_security_groups(security_name)[0]
                assert group
            except boto.exception.EC2ResponseError, e:
                if 'InvalidGroup.NotFound' in e.body:
                    self.security_group = self.conn.create_security_group(
                        security_name,
                        'Authorization to access the buildbot instance.')
                    # Authorize the master as necessary
                    # TODO this is where we'd open the hole to do the reverse pb
                    # connect to the buildbot
                    # ip = urllib.urlopen(
                    #     'http://checkip.amazonaws.com').read().strip()
                    # self.security_group.authorize('tcp', 22, 22, '%s/32' % ip)
                    # self.security_group.authorize('tcp', 80, 80, '%s/32' % ip)
                else:
                    raise

        # get the image
        if self.ami is not None:
            self.image = self.conn.get_image(self.ami)
        else:
            # verify we have access to at least one acceptable image
            discard = self.get_image()
            assert discard

        # get the specified elastic IP, if any
        if elastic_ip is not None:
            elastic_ip = self.conn.get_all_addresses([elastic_ip])[0]
        self.elastic_ip = elastic_ip
        self.subnet_id = subnet_id
        self.security_group_ids = security_group_ids
        self.classic_security_groups = [self.security_name] if self.security_name else None
        self.tags = tags
        self.block_device_map = self.create_block_device_mapping(block_device_map)

    def create_block_device_mapping(self, mapping_definitions):
        if not mapping_definitions:
            return None

        result = BlockDeviceMapping()
        for device_name, device_properties in mapping_definitions.iteritems():
            modified_device_properties = dict(device_properties)
            # Since latent slaves are ephemeral, not leaking volumes on termination
            # is a much safer default.
            if 'delete_on_termination' not in modified_device_properties:
                modified_device_properties['delete_on_termination'] = True
            result[device_name] = BlockDeviceType(**modified_device_properties)
        return result

    def get_image(self):
        if self.image is not None:
            return self.image
        if self.valid_ami_location_regex:
            level = 0
            options = []
            get_match = re.compile(self.valid_ami_location_regex).match
            for image in self.conn.get_all_images(
                    owners=self.valid_ami_owners):
                # Image must be available
                if image.state != 'available':
                    continue
                # Image must match regex
                match = get_match(image.location)
                if not match:
                    continue
                # Gather sorting information
                alpha_sort = int_sort = None
                if level < 2:
                    try:
                        alpha_sort = match.group(1)
                    except IndexError:
                        level = 2
                    else:
                        if level == 0:
                            try:
                                int_sort = int(alpha_sort)
                            except ValueError:
                                level = 1
                options.append([int_sort, alpha_sort,
                                image.location, image.id, image])
            if level:
                log.msg('sorting images at level %d' % level)
                options = [candidate[level:] for candidate in options]
        else:
            options = [(image.location, image.id, image) for image
                       in self.conn.get_all_images(
                           owners=self.valid_ami_owners)]
        options.sort()
        log.msg('sorted images (last is chosen): %s' %
                (', '.join(
                    ['%s (%s)' % (candidate[-1].id, candidate[-1].location)
                     for candidate in options])))
        if not options:
            raise ValueError('no available images match constraints')
        return options[-1][-1]

    def dns(self):
        if self.instance is None:
            return None
        return self.instance.public_dns_name
    dns = property(dns)

    def start_instance(self, build):
        if self.instance is not None:
            raise ValueError('instance active')
        if self.spot_instance:
            return threads.deferToThread(self._request_spot_instance)
        else:
            return threads.deferToThread(self._start_instance)

    def _start_instance(self):
        image = self.get_image()
        reservation = image.run(
            key_name=self.keypair_name, security_groups=self.classic_security_groups,
            instance_type=self.instance_type, user_data=self.user_data,
            placement=self.placement, subnet_id=self.subnet_id,
            security_group_ids=self.security_group_ids,
            block_device_map=self.block_device_map)
        self.instance = reservation.instances[0]
        instance_id, image_id, start_time = self._wait_for_instance(
            reservation)
        if None not in [instance_id, image_id, start_time]:
            return [instance_id, image_id, start_time]
        else:
            log.msg('%s %s failed to start instance %s (%s)' %
                    (self.__class__.__name__, self.slavename,
                     self.instance.id, self.instance.state))
            raise interfaces.LatentBuildSlaveFailedToSubstantiate(
                self.instance.id, self.instance.state)

    def stop_instance(self, fast=False):
        if self.instance is None:
            # be gentle.  Something may just be trying to alert us that an
            # instance never attached, and it's because, somehow, we never
            # started.
            return defer.succeed(None)
        instance = self.instance
        self.output = self.instance = None
        return threads.deferToThread(
            self._stop_instance, instance, fast)

    def _handle_delete_on_term(self):
        if self.delete_vol_term is False:
            return

        block_map = self.conn.get_instance_attribute(self.instance.id,
            attribute='blockDeviceMapping')

        del_on_term = []
        for devname in block_map['blockDeviceMapping']:
            del_on_term.append('%s=true' % devname)

        if del_on_term:
            log.msg(str(del_on_term))
            if not self.conn.modify_instance_attribute(self.instance.id, 'blockDeviceMapping', del_on_term):
                log.msg("Failed to set deletion on termination")

    def _create_volumes(self):
        for device_node, volume_size, region in self.create_volumes:
            new_vol = self.conn.create_volume(volume_size, region)
            duration = 0
            interval = self._poll_resolution

            vol = self.conn.get_all_volumes([new_vol.id])[0]
            while vol.status not in (VOLUME_AVAILABLE):
                time.sleep(interval)
                duration += interval
                if duration % 60 == 0:
                    log.msg(
                        '%s %s has waited %d minutes for volume %s creation' %
                        (self.__class__.__name__, self.slavename,
                        duration // 60, new_vol))
                try:
                    vol = self.conn.get_all_volumes([new_vol.id])[0]
                except boto.exception.EC2ResponseError, e:
                    log.msg('%s %s failed to get all volumes' %
                        (self.__class__.__name__, self.slavename))
                    raise

            log.msg('Attaching EBS volume %s to %s.' % (vol.id, device_node))
            self.conn.attach_volume(vol.id, self.instance.id, device_node)

    def _attach_volumes(self):
        for volume_id, device_node in self.volumes:
            self.conn.attach_volume(volume_id, self.instance.id, device_node)
            log.msg('Attaching EBS volume %s to %s.' %
                    (volume_id, device_node))

    def _handle_volumes(self):
        if len(self.create_volumes) > 0:
            self._create_volumes()

        self._handle_delete_on_term()

        if len(self.volumes) > 0:
            self._attach_volumes()

        block_map = self.conn.get_instance_attribute(self.instance.id,
            attribute='blockDeviceMapping')
        log.msg("%s: blockDeviceMapping=%s" % (self.instance.id, block_map))

    def _stop_instance(self, instance, fast):
        if self.elastic_ip is not None:
            self.conn.disassociate_address(self.elastic_ip.public_ip)
        try:
            instance.update()
        except boto.exception.EC2ResponseError, e:
            log.msg('%s %s cannot find instance %s to terminate' %
                    (self.__class__.__name__, self.slavename, instance.id))
            if e.error_code == 'InvalidInstanceID.NotFound':
                return
            else:
                raise
        if instance.state not in (SHUTTINGDOWN, TERMINATED):
            instance.terminate()
            log.msg('%s %s terminating instance %s' %
                    (self.__class__.__name__, self.slavename, instance.id))
        duration = 0
        interval = self._poll_resolution
        if fast:
            goal = (SHUTTINGDOWN, TERMINATED)
            instance.update()
        else:
            goal = (TERMINATED,)
        while instance.state not in goal:
            time.sleep(interval)
            duration += interval
            if duration % 60 == 0:
                log.msg(
                    '%s %s has waited %d minutes for instance %s to end' %
                    (self.__class__.__name__, self.slavename, duration // 60,
                     instance.id))
            instance.update()
        log.msg('%s %s instance %s %s '
                'after about %d minutes %d seconds' %
                (self.__class__.__name__, self.slavename,
                 instance.id, goal, duration // 60, duration % 60))

    def _request_spot_instance(self):
        timestamp_yesterday = time.gmtime(int(time.time() - 86400))
        spot_history_starttime = time.strftime(
            '%Y-%m-%dT%H:%M:%SZ', timestamp_yesterday)
        spot_prices = self.conn.get_spot_price_history(
            start_time=spot_history_starttime,
            product_description='Linux/UNIX (Amazon VPC)',
            availability_zone=self.placement)
        price_sum = 0.0
        price_count = 0
        for price in spot_prices:
            if price.instance_type == self.instance_type:
                price_sum += price.price
                price_count += 1
        if price_count == 0:
            target_price = 0.02
        else:
            target_price = (price_sum / price_count) * self.price_multiplier
        if target_price > self.max_spot_price:
            log.msg('%s %s calculated spot price %0.2f exceeds '
                    'configured maximum of %0.2f' %
                    (self.__class__.__name__, self.slavename,
                     target_price, self.max_spot_price))
            raise interfaces.LatentBuildSlaveFailedToSubstantiate()
        else:
            log.msg('%s %s requesting spot instance with price %0.2f.' %
                    (self.__class__.__name__, self.slavename, target_price))
        reservations = self.conn.request_spot_instances(
            target_price, self.ami, key_name=self.keypair_name,
            security_groups=self.classic_security_groups,
            instance_type=self.instance_type,
            user_data=self.user_data,
            placement=self.placement,
            subnet_id=self.subnet_id,
            security_group_ids=self.security_group_ids,
            block_device_map=self.block_device_map)
        request = self._wait_for_request(reservations[0])
        instance_id = request.instance_id
        reservations = self.conn.get_all_instances(instance_ids=[instance_id])
        self.instance = reservations[0].instances[0]
        return self._wait_for_instance(self.get_image())

    def _wait_for_instance(self, image):
        log.msg('%s %s waiting for instance %s to start' %
                (self.__class__.__name__, self.slavename, self.instance.id))
        duration = 0
        interval = self._poll_resolution
        while self.instance.state == PENDING:
            time.sleep(interval)
            duration += interval
            if duration % 60 == 0:
                log.msg('%s %s has waited %d minutes for instance %s' %
                        (self.__class__.__name__, self.slavename, duration // 60,
                         self.instance.id))
            try:
                self.instance.update()
            except boto.exception.EC2ResponseError, e:
                log.msg('%s %s failed to find instance %s' %
                        (self.__class__.__name__, self.slavename,
                         self.instance.id))
                if e.error_code == 'InvalidInstanceID.NotFound':
                    continue
                else:
                    raise
        if self.instance.state == RUNNING:
            self.output = self.instance.get_console_output()
            minutes = duration // 60
            seconds = duration % 60
            log.msg('%s %s instance %s started on %s '
                    'in about %d minutes %d seconds (%s)' %
                    (self.__class__.__name__, self.slavename,
                     self.instance.id, self.dns, minutes, seconds,
                     self.output.output))
            if self.elastic_ip is not None:
                self.instance.use_ip(self.elastic_ip)
            start_time = '%02d:%02d:%02d' % (
                minutes // 60, minutes % 60, seconds)
            self._handle_volumes()
            if len(self.tags) > 0:
                self.conn.create_tags(self.instance.id, self.tags)
            return self.instance.id, image.id, start_time
        else:
            return None, None, None

    def _wait_for_request(self, reservation):
        log.msg('%s %s requesting spot instance' %
                (self.__class__.__name__, self.slavename))
        duration = 0
        attempts = 0
        interval = self._poll_resolution
        while attempts < self._poll_retry:
            try:
                requests = self.conn.get_all_spot_instance_requests(
                    request_ids=[reservation.id])
                request = requests[0]
                request_status = request.status.code
                break
            except boto.exception.EC2ResponseError, e:
                attempts += 1
                log.msg('%s %s failed to find spot request %s' %
                        (self.__class__.__name__, self.slavename,
                        reservation.id))
                if e.error_code == 'InvalidSpotInstanceRequestID.NotFound':
                    time.sleep(interval)
                    continue
                else:
                    raise
        while request_status in SPOT_REQUEST_PENDING_STATES:
            time.sleep(interval)
            duration += interval
            if duration % 60 == 0:
                log.msg('%s %s has waited %d minutes for spot request %s' %
                        (self.__class__.__name__, self.slavename, duration // 60,
                         request.id))
            try:
                requests = self.conn.get_all_spot_instance_requests(
                    request_ids=[request.id])
                request = requests[0]
                request_status = request.status.code
            except boto.exception.EC2ResponseError, e:
                log.msg('%s %s failed to find spot request %s' %
                        (self.__class__.__name__, self.slavename, request.id))
                if e.error_code == 'InvalidSpotInstanceRequestID.NotFound':
                    continue
                else:
                    raise
        if request_status == FULFILLED:
            minutes = duration // 60
            seconds = duration % 60
            log.msg('%s %s spot request %s fulfilled '
                    'in about %d minutes %d seconds' %
                    (self.__class__.__name__, self.slavename,
                     request.id, minutes, seconds))
            return request
        elif request_status == PRICE_TOO_LOW:
            log.msg('%s %s spot request rejected, spot price too low' %
                    (self.__class__.__name__, self.slavename))
            raise interfaces.LatentBuildSlaveFailedToSubstantiate(
                request.id, request.status)
        else:
            log.msg('%s %s failed to fulfill spot request %s with status %s' %
                    (self.__class__.__name__, self.slavename,
                     request.id, request_status))
            raise interfaces.LatentBuildSlaveFailedToSubstantiate(
                request.id, request.status)
