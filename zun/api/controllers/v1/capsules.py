#    Copyright 2017 ARM Holdings.
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

from oslo_log import log as logging
from oslo_utils import strutils
from oslo_utils import uuidutils
import pecan
import six
from zun.api.controllers import base
from zun.api.controllers import link
from zun.api.controllers.v1 import collection
from zun.api.controllers.v1.schemas import capsules as schema
from zun.api.controllers.v1.views import capsules_view as view
from zun.api import utils as api_utils
from zun.common import consts
from zun.common import exception
from zun.common import name_generator
from zun.common import policy
from zun.common import utils
from zun.common import validation
from zun import objects

LOG = logging.getLogger(__name__)


def _get_capsule(capsule_id):
    capsule = api_utils.get_resource('Capsule', capsule_id)
    if not capsule:
        pecan.abort(404, ('Not found; the container you requested '
                          'does not exist.'))
    return capsule


def _get_container(container_id):
    container = api_utils.get_resource('Container', container_id)
    if not container:
        pecan.abort(404, ('Not found; the container you requested '
                          'does not exist.'))
    return container


def check_policy_on_capsule(capsule, action):
    context = pecan.request.context
    policy.enforce(context, action, capsule, action=action)


class CapsuleCollection(collection.Collection):
    """API representation of a collection of Capsules."""

    fields = {
        'capsules',
        'next'
    }

    """A list containing capsules objects"""

    def __init__(self, **kwargs):
        self._type = 'capsules'

    @staticmethod
    def convert_with_links(rpc_capsules, limit, url=None,
                           expand=False, **kwargs):
        collection = CapsuleCollection()
        collection.capsules = \
            [view.format_capsule(url, p) for p in rpc_capsules]
        collection.next = collection.get_next(limit, url=url, **kwargs)
        return collection


class CapsuleController(base.Controller):
    '''Controller for Capsules'''

    _custom_actions = {

    }

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_all(self, **kwargs):
        '''Retrieve a list of capsules.'''
        context = pecan.request.context
        policy.enforce(context, "capsule:get_all",
                       action="capsule:get_all")
        return self._get_capsules_collection(**kwargs)

    def _get_capsules_collection(self, **kwargs):
        context = pecan.request.context
        all_tenants = kwargs.get('all_tenants')
        if all_tenants:
            try:
                all_tenants = strutils.bool_from_string(all_tenants, True)
            except ValueError as err:
                raise exception.InvalidInput(six.text_type(err))
        else:
            # If no value, it's considered to disable all_tenants
            all_tenants = False
        if all_tenants:
            context.all_tenants = True
        compute_api = pecan.request.compute_api
        limit = api_utils.validate_limit(kwargs.get('limit'))
        sort_dir = api_utils.validate_sort_dir(kwargs.get('sort_dir', 'asc'))
        sort_key = kwargs.get('sort_key', 'id')
        resource_url = kwargs.get('resource_url')
        expand = kwargs.get('expand')
        filters = None
        marker_obj = None
        marker = kwargs.get('marker')
        if marker:
            marker_obj = objects.Capsule.get_by_uuid(context,
                                                     marker)
        capsules = objects.Capsule.list(context,
                                        limit,
                                        marker_obj,
                                        sort_key,
                                        sort_dir,
                                        filters=filters)

        # Sync status for container inside capsule
        for i, c in enumerate(capsules):
            try:
                containers_list = c.containers_uuids
                if containers_list is not None:
                    # Capsule is depending on infra container status
                    uuid = containers_list[0]
                    container = _get_container(uuid)
                    container = compute_api.container_show(context, container)
                    c.status = container.status
                    c.save(context)

                capsules[i] = c
            except Exception as e:
                LOG.exception(("Error while list capsule %(uuid)s: "
                               "%(e)s."),
                              {'uuid': c.uuid, 'e': e})
                capsules[i].status = consts.UNKNOWN

        return CapsuleCollection.convert_with_links(capsules, limit,
                                                    url=resource_url,
                                                    expand=expand,
                                                    sort_key=sort_key,
                                                    sort_dir=sort_dir)

    @pecan.expose('json')
    @api_utils.enforce_content_types(['application/json'])
    @exception.wrap_pecan_controller_exception
    @validation.validated(schema.capsule_create)
    def post(self, **capsule_dict):
        """Create a new capsule.

        :param capsule: a capsule within the request body.
        """
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        policy.enforce(context, "capsule:create",
                       action="capsule:create")
        capsule_dict['capsule_version'] = 'alpha'
        capsule_dict['kind'] = 'capsule'

        capsules_spec = capsule_dict['spec']
        containers_spec = utils.check_capsule_template(capsules_spec)
        capsule_dict['uuid'] = uuidutils.generate_uuid()
        new_capsule = objects.Capsule(context, **capsule_dict)
        new_capsule.create(context)
        new_capsule.containers = []
        new_capsule.containers_uuids = []
        new_capsule.volumes = []
        count = len(containers_spec)

        capsule_restart_policy = capsules_spec.get('restartPolicy', 'always')

        metadata_info = capsules_spec.get('metadata', None)
        if metadata_info:
            new_capsule.meta_name = metadata_info.get('name', None)
            new_capsule.meta_labels = metadata_info.get('labels', None)

        # Generate Object for infra container
        sandbox_container = objects.Container(context)
        sandbox_container.project_id = context.project_id
        sandbox_container.user_id = context.user_id
        sandbox_container.create(context)
        new_capsule.containers.append(sandbox_container)
        new_capsule.containers_uuids.append(sandbox_container.uuid)

        for k in range(0, count):
            container_dict = containers_spec[k]
            container_dict['project_id'] = context.project_id
            container_dict['user_id'] = context.user_id
            name = self._generate_name_for_container_inside_capsule(
                capsule_dict['uuid'])
            container_dict['name'] = name

            # Valiadtion accepts 'None' so need to convert it to None
            container_dict = self._transfer_different_field('imageDriver',
                                                            'image_driver',
                                                            **container_dict)
            container_dict = \
                self._transfer_different_field('imagePullPolicy',
                                               'image_pull_policy',
                                               **container_dict)
            container_dict = self._transfer_different_field('workDir',
                                                            'workdir',
                                                            **container_dict)
            container_dict = self._transfer_different_field('env',
                                                            'environment',
                                                            **container_dict)

            if container_dict.get('args', None) and \
                    container_dict.get('command', None):
                container_dict = self._transfer_list_to_str(container_dict,
                                                            'command')
                container_dict = self._transfer_list_to_str(container_dict,
                                                            'args')
                container_dict['command'] = \
                    container_dict['command'] + ' ' + container_dict['args']
                container_dict.pop('args')
            elif container_dict.get('command', None):
                container_dict = self._transfer_list_to_str(container_dict,
                                                            'command')
            elif container_dict.get('args', None):
                container_dict = self._transfer_list_to_str(container_dict,
                                                            'args')
                container_dict['command'] = container_dict['args']
                container_dict.pop('args')

            # NOTE(kevinz): Don't support pod remapping, will find a
            # easy way to implement thie.
            # if container need to open some port, just open it in container,
            # user can change the security group and getting access to port.
            if container_dict.get('ports'):
                container_dict.pop('ports')

            if container_dict.get('resources'):
                resources_list = container_dict.get('resources')
                allocation = resources_list.get('allocation')
                if allocation.get('cpu'):
                    container_dict['cpu'] = allocation.get('cpu')
                if allocation.get('memory'):
                    container_dict['memory'] = \
                        str(allocation['memory']) + 'M'
                container_dict.pop('resources')

            # NOTE(mkrai): Intent here is to check the existence of image
            # before proceeding to create container. If image is not found,
            # container create will fail with 400 status.
            images = compute_api.image_search(
                context,
                container_dict['image'],
                container_dict.get('image_driver'),
                True)
            if not images:
                raise exception.ImageNotFound(image=container_dict['image'])

            if capsule_restart_policy:
                container_dict['restart_policy'] = \
                    {"MaximumRetryCount": "0",
                     "Name": capsule_restart_policy}
                self._check_for_restart_policy(container_dict)

            container_dict['status'] = consts.CREATING
            new_container = objects.Container(context, **container_dict)
            new_container.create(context)
            new_capsule.containers.append(new_container)
            new_capsule.containers_uuids.append(new_container.uuid)

        new_capsule.save(context)
        compute_api.capsule_create(context, new_capsule)
        # Set the HTTP Location Header
        pecan.response.location = link.build_url('capsules',
                                                 new_capsule.uuid)

        pecan.response.status = 202
        return view.format_capsule(pecan.request.host_url, new_capsule)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def get_one(self, capsule_id):
        """Retrieve information about the given capsule.

        :param capsule_ident: UUID or name of a capsule.
        """
        capsule = _get_capsule(capsule_id)
        check_policy_on_capsule(capsule.as_dict(), "capsule:get")
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        sandbox = _get_container(capsule.containers_uuids[0])

        try:
            container = compute_api.container_show(context, sandbox)
            capsule.status = container.status
            capsule.save(context)
        except Exception as e:
            LOG.exception(("Error while show capsule %(uuid)s: "
                           "%(e)s."),
                          {'uuid': capsule.uuid, 'e': e})
            capsule.status = consts.UNKNOWN
        return view.format_capsule(pecan.request.host_url, capsule)

    @pecan.expose('json')
    @exception.wrap_pecan_controller_exception
    def delete(self, capsule_id, force=False):
        """Delete a capsule.

        :param capsule_ident: UUID or Name of a capsule.
        """
        capsule = _get_capsule(capsule_id)
        check_policy_on_capsule(capsule.as_dict(), "capsule:delete")
        try:
            force = strutils.bool_from_string(force, strict=True)
        except ValueError:
            msg = _('Valid force values are true, false, 0, 1, yes and no')
            raise exception.InvalidValue(msg)

        if not force:
            utils.validate_container_state(capsule, 'delete')
        else:
            utils.validate_container_state(capsule, 'delete_force')
        containers_num = len(capsule.containers_uuids)
        context = pecan.request.context
        compute_api = pecan.request.compute_api
        capsule.task_state = consts.CONTAINER_DELETING
        capsule.save(context)
        for k in range(0, containers_num):
            container = _get_container(capsule.containers_uuids[k])
            compute_api.container_delete(context, container, force)
        capsule.task_state = None
        capsule.save(context)
        capsule.destroy(context)
        pecan.response.status = 204

    def _generate_name_for_container_inside_capsule(self, capsule_uuid=None):
        '''Generate a random name like: zeta-22-container.'''
        name_gen = name_generator.NameGenerator()
        name = name_gen.generate()
        return 'capsule-' + capsule_uuid + '-' + name

    def _transfer_different_field(self, field_tpl,
                                  field_container, **container_dict):
        '''Transfer the template specified field to container_field'''
        if container_dict.get(field_tpl):
            container_dict[field_container] = api_utils.string_or_none(
                container_dict.get(field_tpl))
            container_dict.pop(field_tpl)
        return container_dict

    def _check_for_restart_policy(self, container_dict):
        '''Check for restart policy input'''
        restart_policy = container_dict.get('restart_policy')
        if not restart_policy:
            return

        name = restart_policy.get('Name')
        num = restart_policy.setdefault('MaximumRetryCount', '0')
        count = int(num)
        if name in ['unless-stopped', 'always']:
            if count != 0:
                raise exception.InvalidValue(("maximum retry "
                                              "count not valid "
                                              "with restart policy "
                                              "of %s") % name)
        elif name in ['no']:
            container_dict.get('restart_policy')['MaximumRetryCount'] = '0'

    def _transfer_list_to_str(self, container_dict, field):
        if container_dict[field]:
            dict = None
            for k in range(0, len(container_dict[field])):
                if dict:
                    dict = dict + ' ' + container_dict[field][k]
                else:
                    dict = container_dict[field][k]
            container_dict[field] = dict
        return container_dict
