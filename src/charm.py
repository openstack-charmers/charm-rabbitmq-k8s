#!/usr/bin/env python3
#
# Copyright 2021 David Ames
# Copyright 2021 Canonical Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""RabbitMQ Operator Charm."""

import collections
import logging
import textwrap
from ipaddress import (
    IPv4Address,
    IPv6Address,
)
from typing import (
    Dict,
    List,
    Union,
)

import ops
import pwgen
import requests
import tenacity
from charms.loki_k8s.v1.loki_push_api import (
    LogForwarder,
)
from charms.rabbitmq_k8s.v0.rabbitmq import (
    RabbitMQProvides,
)
from charms.traefik_k8s.v1.ingress import (
    IngressPerAppRequirer,
)
from lightkube.core.client import (
    Client,
)
from lightkube.models.core_v1 import (
    ServicePort,
    ServiceSpec,
)
from lightkube.models.meta_v1 import (
    ObjectMeta,
)
from lightkube.resources.core_v1 import (
    Service,
)
from lightkube_extensions.batch import (
    KubernetesResourceManager,
    create_charm_default_labels,
)
from ops.charm import (
    ActionEvent,
    CharmBase,
    PebbleCustomNoticeEvent,
)
from ops.framework import (
    EventBase,
    StoredState,
)
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    ModelError,
    Relation,
    WaitingStatus,
)
from ops.pebble import (
    ExecError,
    PathError,
)

import interface_rabbitmq_peers
import rabbit_extended_api

logger = logging.getLogger(__name__)

RABBITMQ_CONTAINER = "rabbitmq"
RABBITMQ_SERVICE = "rabbitmq"
RABBITMQ_USER = "rabbitmq"
RABBITMQ_GROUP = "rabbitmq"
RABBITMQ_DATA_DIR = "/var/lib/rabbitmq"
RABBITMQ_COOKIE_PATH = "/var/lib/rabbitmq/.erlang.cookie"

SELECTOR_ALL = "all"
SELECTOR_NONE = "none"
SELECTOR_EVEN = "even"
SELECTOR_INDIVIDUAL = "individual"

EPMD_SERVICE = "epmd"
NOTIFIER_SERVICE = "notifier"
TIMER_NOTICE = "rabbitmq.local/timer"

LB_LABEL = "rabbitmq-loadbalancer"
RABBITMQ_SERVICE_PORT = 5672
RABBITMQ_MANAGEMENT_PORT = 15672


class RabbitOperatorError(Exception):
    """Common base class for all RabbitMQ Operator exceptions."""


class RabbitMQOperatorCharm(CharmBase):
    """RabbitMQ Operator Charm."""

    _stored = StoredState()
    _operator_user = "operator"

    def __init__(self, *args):
        super().__init__(*args)

        self._lightkube_client = None
        self._lightkube_field_manager: str = self.app.name
        self._lb_name: str = f"{self.app.name}-lb"

        # Open ports on default cluster IP
        # OpenStack services uses cluster IP from service rabbitmq
        self.unit.set_ports(RABBITMQ_SERVICE_PORT, RABBITMQ_MANAGEMENT_PORT)

        # As the service ports are not dynamic, loadbalancer need not
        # be created/patched on upgrade-hook or update-status hook
        # In case the service ports are modified, _reconcile_lb should
        # run on upgrade-hook
        # NOTE(jamespage): This should become part of what Juju
        # does at some point in time.
        self.framework.observe(self.on.install, self._reconcile_lb)
        self.framework.observe(
            self.on.rabbitmq_pebble_ready, self._on_config_changed
        )
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(
            self.on.get_operator_info_action, self._on_get_operator_info_action
        )
        self.framework.observe(
            self.on.add_member_action, self._on_add_member_action
        )
        self.framework.observe(
            self.on.delete_member_action, self._on_delete_member_action
        )
        self.framework.observe(self.on.update_status, self._on_update_status)
        # Peers
        self.peers = interface_rabbitmq_peers.RabbitMQOperatorPeers(
            self, "peers"
        )
        self.framework.observe(
            self.peers.on.connected,
            self._on_peer_relation_connected,
        )
        self.framework.observe(
            self.peers.on.ready,
            self._on_peer_relation_ready,
        )
        self.framework.observe(
            self.peers.on.leaving,
            self._on_peer_relation_leaving,
        )
        # AMQP Provides
        self.amqp_provider = RabbitMQProvides(
            self, "amqp", self.create_amqp_credentials
        )
        self.framework.observe(
            self.amqp_provider.on.ready_amqp_clients,
            self._on_ready_amqp_clients,
        )
        self.framework.observe(
            self.amqp_provider.on.gone_away_amqp_clients,
            self._on_gone_away_amqp_clients,
        )
        self.framework.observe(self.on.remove, self._on_remove)

        self._stored.set_default(enabled_plugins=[])
        self._stored.set_default(rabbitmq_version=None)

        self._enable_plugin("rabbitmq_management")
        self._enable_plugin("rabbitmq_peer_discovery_k8s")

        # NOTE: ingress for management WebUI/API only
        self.management_ingress = IngressPerAppRequirer(
            self,
            "ingress",
            port=RABBITMQ_MANAGEMENT_PORT,
        )
        # Logging relation
        self.logging = LogForwarder(self, relation_name="logging")

        self.framework.observe(
            self.on.get_service_account_action, self._get_service_account
        )

        self.framework.observe(
            self.on.ensure_queue_ha_action, self._ensure_queue_ha_action
        )

        self.framework.observe(
            self.on[RABBITMQ_CONTAINER].pebble_custom_notice,
            self._on_pebble_custom_notice,
        )

    def _pebble_ready(self) -> bool:
        """Check whether RabbitMQ container is up and configurable."""
        return self.unit.get_container(RABBITMQ_CONTAINER).can_connect()

    def _rabbitmq_running(self) -> bool:
        """Check whether RabbitMQ service is running."""
        if self._pebble_ready():
            try:
                return (
                    self.unit.get_container(RABBITMQ_CONTAINER)
                    .get_service(RABBITMQ_SERVICE)
                    .is_running()
                )
            except ModelError:
                return False
        return False

    def _on_config_changed(self, event: EventBase) -> None:
        """Update configuration for RabbitMQ."""
        # Ensure rabbitmq container is up and pebble is ready
        if not self._pebble_ready():
            event.defer()
            return

        # Ensure that erlang cookie is consistent across units
        if not self.unit.is_leader() and not self.peers.erlang_cookie:
            event.defer()
            return

        # Wait for the peers binding address to be ready before configuring
        # the rabbit environment. This is due to rabbitmq-env.conf needing
        # the unit address for peering.
        if self.peers_bind_address is None:
            logger.debug("Waiting for binding address on peers interface")
            event.defer()
            return

        # Change ownership of /var/lib/rabbitmq
        self._set_ownership_on_data_dir()

        # Render and push configuration files
        self._render_and_push_config_files()

        # Render and push notifier script
        notifier_changed = self._render_and_push_pebble_notifier()

        # Get the rabbitmq container so we can configure/manipulate it
        container = self.unit.get_container(RABBITMQ_CONTAINER)

        # Ensure erlang cookie is consistent
        if self.peers.erlang_cookie:
            container.push(
                RABBITMQ_COOKIE_PATH,
                self.peers.erlang_cookie,
                permissions=0o600,
                make_dirs=True,
                user=RABBITMQ_USER,
                group=RABBITMQ_GROUP,
            )

        # Add initial Pebble config layer using the Pebble API
        container.add_layer("rabbitmq", self._rabbitmq_layer(), combine=True)

        # Autostart any services that were defined with startup: enabled
        if not container.get_service(RABBITMQ_SERVICE).is_running():
            logging.info("Autostarting rabbitmq")
            container.autostart()
        else:
            logging.debug("RabbitMQ service is running")

        # If the notifier script has changed, restart the notifier service
        if notifier_changed:
            container.restart(NOTIFIER_SERVICE)

        @tenacity.retry(
            wait=tenacity.wait_exponential(multiplier=1, min=4, max=10)
        )
        def _check_rmq_running():
            logging.info("Waiting for RabbitMQ to start")
            if not self.rabbit_running:
                raise tenacity.TryAgain()
            else:
                logging.info("RabbitMQ started")

        _check_rmq_running()
        self._on_update_status(event)

    def _rabbitmq_layer(self) -> dict:
        """Pebble layer definition for RabbitMQ."""
        # NOTE(jamespage)
        # Use the full path to the rabbitmq-server binary
        # rather than the helper wrapper script to avoid
        # redirection of console output to a log file.
        return {
            "summary": "RabbitMQ layer",
            "description": "pebble config layer for RabbitMQ",
            "services": {
                RABBITMQ_SERVICE: {
                    "override": "replace",
                    "summary": "RabbitMQ Server",
                    "command": "/usr/lib/rabbitmq/bin/rabbitmq-server",
                    "startup": "enabled",
                    "user": RABBITMQ_USER,
                    "group": RABBITMQ_GROUP,
                    "requires": [EPMD_SERVICE],
                },
                EPMD_SERVICE: {
                    "override": "replace",
                    "summary": "Erlang EPM service",
                    "command": "epmd -d",
                    "startup": "enabled",
                    "user": RABBITMQ_USER,
                    "group": RABBITMQ_GROUP,
                },
                NOTIFIER_SERVICE: {
                    "override": "replace",
                    "summary": "Pebble notifier",
                    "command": "/usr/bin/notifier",
                    "startup": "enabled",
                    # Workaround to avoid bug
                    # https://github.com/canonical/pebble/issues/525
                    "requires": [RABBITMQ_SERVICE],
                },
            },
        }

    def _on_peer_relation_leaving(  # noqa: C901
        self, event: EventBase
    ) -> None:
        if self.unit.is_leader():
            leaving_node = self.generate_nodename(event.nodename)
            container = self.unit.get_container(RABBITMQ_CONTAINER)
            logging.info(f"Removing {leaving_node} from queues")
            try:
                # forget_cluster_node not currently supported by HTTP API
                process = container.exec(
                    ["rabbitmqctl", "forget_cluster_node", leaving_node],
                    timeout=5 * 60,
                )
                output, _ = process.wait_output()
                logging.info(output)
            except ExecError as e:
                if "The node selected is not in the cluster" in e.stderr:
                    logging.warning(
                        f"Removal of {leaving_node} failed, node not found"
                    )
                else:
                    logging.error(f"Removal of {leaving_node} failed")
                    logging.error(e.stdout)
                    logging.error(e.stderr)

    # TODO: refactor this method to reduce complexity.
    def _on_peer_relation_connected(  # noqa: C901
        self, event: EventBase
    ) -> None:
        """Event handler on peers relation created."""
        # Defer any peer relation setup until RMQ is actually running
        if not self._rabbitmq_running():
            event.defer()
            return

        if self.peers_bind_address is None:
            event.defer()
            return

        logging.info("Peer relation instantiated.")

        if not self.peers.erlang_cookie and self.unit.is_leader():
            logging.debug("Providing erlang cookie to cluster")
            container = self.unit.get_container(RABBITMQ_CONTAINER)
            try:
                with container.pull(RABBITMQ_COOKIE_PATH) as cfile:
                    erlang_cookie = cfile.read().rstrip()
                self.peers.set_erlang_cookie(erlang_cookie)
            except PathError:
                logging.debug(
                    "RabbitMQ not started, deferring cookie provision"
                )
                event.defer()

        if not self.peers.operator_user_created and self.unit.is_leader():
            logging.debug(
                "Attempting to initialize from on peers relation created."
            )
            # Generate the operator user/password
            try:
                self._initialize_operator_user()
                logging.debug("Operator user initialized.")
            except requests.exceptions.HTTPError as e:
                if e.errno == 401:
                    logging.error("Authorization failed")
                    raise e
            except requests.exceptions.ConnectionError as e:
                if "Caused by NewConnectionError" in e.__str__():
                    logging.warning(
                        "RabbitMQ is not ready. Deferring. {}".format(
                            e.__str__()
                        )
                    )
                    event.defer()
                else:
                    raise e

        self._on_update_status(event)

    def get_queue_growth_selector(self, min_q_len: int, max_q_len: int):
        """Select a queue growth strategy.

        Select a queue growth strategy from:
            ALL: All queues add a new replica
            NONE: No queues have additional replica added
            EVEN: Queues with an even number of replicas have additional replica added
            INDIVIDUAL: Each queue is expanded individually

        NOTE: INDIVIDUAL is expensive as an api call needs to be made
              for each queue.
        """
        if min_q_len == max_q_len:
            if max_q_len < self.min_replicas():
                # 1 -> 2
                # 2 -> 3
                selector = SELECTOR_ALL
            else:
                # All queues have enough members but queues should
                # not have an even number of replicas
                selector = SELECTOR_EVEN
        elif min_q_len > 1:
            # 2->3
            # 3->3
            # 4->5 (no even queues)
            selector = SELECTOR_EVEN
        elif min_q_len == 1:
            if max_q_len < self.min_replicas():
                # 1 -> 2
                # 2 -> 3
                selector = SELECTOR_ALL
            else:
                # Cannot use "even" as the queues with 1 node need expanding,
                # cannot use "all" as there are queues with 3+ members
                selector = SELECTOR_INDIVIDUAL
        return selector

    def unit_in_cluster(self, unit: str) -> bool:
        """Is unit in cluster according to rabbit api."""
        api = self._get_admin_api()
        joining_node = self.generate_nodename(unit)
        clustered_nodes = [n["name"] for n in api.list_nodes()]
        logging.debug(f"Found cluster nodes {clustered_nodes}")
        return joining_node in clustered_nodes

    def grow_queues_onto_unit(self, unit) -> None:
        """Grow any undersized queues onto unit."""
        api = self._get_admin_api()
        joining_node = self.generate_nodename(unit)
        queue_members = [len(q["members"]) for q in api.list_quorum_queues()]
        if not queue_members:
            logging.debug("No queues found, queue growth skipped")
            return
        queue_members.sort()
        selector = self.get_queue_growth_selector(
            queue_members[0], queue_members[-1]
        )
        logging.debug(f"selector: {selector}")
        if selector in [SELECTOR_ALL, SELECTOR_EVEN]:
            api.grow_queue(joining_node, selector)
        elif selector == SELECTOR_INDIVIDUAL:
            undersized_queues = self.get_undersized_queues()
            for q in undersized_queues:
                if joining_node not in q["members"]:
                    api.add_member(joining_node, q["vhost"], q["name"])
        elif selector == SELECTOR_NONE:
            logging.debug("No queues need new replicas")
        else:
            logging.error(f"Unknown selectore type {selector}")

    def _on_peer_relation_ready(self, event: EventBase) -> None:
        """Event handler on peers relation ready."""
        if not self._rabbitmq_running():
            event.defer()
            return

        if not self.peers.operator_user_created:
            event.defer()
            return

        if not self.unit_in_cluster(event.nodename):
            logging.debug(f"{event.nodename} is not in cluster yet.")
            event.defer()
            return

        if self.unit.is_leader():
            self.grow_queues_onto_unit(event.nodename)
            api = self._get_admin_api()
            api.rebalance_queues()
        self._on_update_status(event)

    def _on_add_member_action(self, event) -> None:
        """Handle add_member charm action."""
        api = self._get_admin_api()
        api.add_member(
            self.generate_nodename(event.params["unit-name"]),
            event.params.get("vhost"),
            event.params["queue-name"],
        )
        self._on_update_status(event)

    def _on_delete_member_action(self, event) -> None:
        """Handle delete_member charm action."""
        api = self._get_admin_api()
        api.delete_member(
            self.generate_nodename(event.params["unit-name"]),
            event.params.get("vhost"),
            event.params["queue-name"],
        )
        self._on_update_status(event)

    def _on_ready_amqp_clients(self, event) -> None:
        """Event handler on AMQP clients ready."""
        self._on_update_status(event)

    def _on_gone_away_amqp_clients(self, event) -> None:
        """Event handler on AMQP clients goneaway."""
        if not self.unit.is_leader():
            logging.debug("Not a leader unit, nothing to do")
            return

        api = self._get_admin_api()
        username = self.amqp_provider.username(event)
        if username and self.does_user_exist(username):
            api.delete_user(username)

        self.peers.delete_user(username)

    def _on_pebble_custom_notice(self, event: PebbleCustomNoticeEvent):
        """Handle pebble custom notice event."""
        if event.notice.key == TIMER_NOTICE:
            if not self.unit.is_leader():
                logger.debug("Not a leader unit, nothing to do")
                return
            if not self._manage_queues():
                logger.debug("Queue management disabled, nothing to do")
                return
            if not self.rabbit_running:
                logger.debug("RabbitMQ not running, deferring")
                event.defer()
                return
            if not self.peers.operator_user_created:
                logger.debug("Operator user not created, deferring")
                event.defer()
                return
            self.ensure_queue_ha()
            self._on_update_status(event)
            return

    @property
    def amqp_rel(self) -> Relation:
        """AMQP relation."""
        for amqp in self.framework.model.relations["amqp"]:
            # We only care about one relation. Returning the first.
            return amqp

    @property
    def peers_bind_address(self) -> str:
        """Bind address for peer interface."""
        return self._peers_bind_address()

    def _peers_bind_address(self) -> str:
        """Bind address for the rabbit node to its peers.

        :returns: IP address to use for peering, or None if not yet available
        """
        address = self.model.get_binding("peers").network.bind_address
        if address is None:
            return address
        return str(address)

    @property
    def amqp_bind_address(self) -> str:
        """AMQP endpoint bind address."""
        return self._amqp_bind_address()

    def _amqp_bind_address(self) -> str:
        """Bind address for AMQP.

        :returns: IP address to use for AMQP endpoint.
        """
        return str(self.model.get_binding("amqp").network.bind_address)

    @property
    def ingress_address(self) -> Union[IPv4Address, IPv6Address]:
        """Network IP address for access to the RabbitMQ service."""
        return self.model.get_binding("amqp").network.ingress_addresses[0]

    def rabbitmq_url(self, username, password, vhost) -> str:
        """URL to access RabbitMQ unit."""
        return (
            f"rabbit://{username}:{password}"
            f"@{self.ingress_address}:{RABBITMQ_SERVICE_PORT}/{vhost}"
        )

    def does_user_exist(self, username: str) -> bool:
        """Does the username exist in RabbitMQ?

        :param username: username to check for
        :returns: whether username was found
        """
        api = self._get_admin_api()
        try:
            api.get_user(username)
        except requests.exceptions.HTTPError as e:
            # Username does not exist
            if e.response.status_code == 404:
                return False
            else:
                raise e
        return True

    def does_vhost_exist(self, vhost: str) -> bool:
        """Does the vhost exist in RabbitMQ?

        :param vhost: vhost to check for
        :returns: whether vhost was found
        """
        api = self._get_admin_api()
        try:
            api.get_vhost(vhost)
        except requests.exceptions.HTTPError as e:
            # Vhost does not exist
            if e.response.status_code == 404:
                return False
            else:
                raise e
        return True

    def create_user(self, username: str) -> str:
        """Create user in rabbitmq.

        Return the password for the user.

        :param username: username to create
        :returns: password for username
        """
        api = self._get_admin_api()
        _password = pwgen.pwgen(12)
        api.create_user(username, _password)
        return _password

    def set_user_permissions(
        self,
        username: str,
        vhost: str,
        configure: str = ".*",
        write: str = ".*",
        read: str = ".*",
    ) -> None:
        """Set permissions for a RabbitMQ user.

        Return the password for the user.

        :param username: User to change permissions for
        :param configure: Configure perms
        :param write: Write perms
        :param read: Read perms
        """
        api = self._get_admin_api()
        api.create_user_permission(
            username, vhost, configure=configure, write=write, read=read
        )

    def create_vhost(self, vhost: str) -> None:
        """Create virtual host in RabbitMQ.

        :param vhost: virtual host to create.
        """
        api = self._get_admin_api()
        api.create_vhost(vhost)

    def _enable_plugin(self, plugin: str) -> None:
        """Enable plugin.

        Enable a RabbitMQ plugin.

        :param plugin: Plugin to enable.
        """
        if plugin not in self._stored.enabled_plugins:
            self._stored.enabled_plugins.append(plugin)

    def _disable_plugin(self, plugin: str) -> None:
        """Disable plugin.

        Disable a RabbitMQ plugin.

        :param plugin: Plugin to disable.
        """
        if plugin in self._stored.enabled_plugins:
            self._stored.enabled_plugins.remove(plugin)

    @property
    def hostname(self) -> str:
        """Hostname for access to RabbitMQ."""
        return f"{self.app.name}-endpoints.{self.model.name}.svc.cluster.local"

    @property
    def _rabbitmq_mgmt_url(self) -> str:
        """Management URL for RabbitMQ."""
        # Use localhost for admin ACL
        return f"http://localhost:{RABBITMQ_MANAGEMENT_PORT}"

    @property
    def _operator_password(self) -> Union[str, None]:
        """Return the operator password.

        If the operator password does not exist on the peer relation, create a
        new one and update the peer relation. It is necessary to store this on
        the peer relation so that it is not lost on any one unit's local
        storage. If the leader is deposed, the new leader continues to have
        administrative access to the message queue.

        :returns: String password or None
        :rtype: Unition[str, None]
        """
        if not self.peers.operator_password and self.unit.is_leader():
            self.peers.set_operator_password(pwgen.pwgen(12))
        return self.peers.operator_password

    @property
    def rabbit_running(self) -> bool:
        """Check whether RabbitMQ is running by accessing its API."""
        try:
            if self.peers.operator_user_created:
                # Use operator once created
                api = self._get_admin_api()
            else:
                # Fallback to guest user during early charm lifecycle
                api = self._get_admin_api("guest", "guest")
            # Cache product version from overview check for later use
            overview = api.overview()
            self._stored.rabbitmq_version = overview.get("product_version")
        except requests.exceptions.ConnectionError:
            return False
        return True

    def _get_admin_api(
        self, username: str = None, password: str = None
    ) -> rabbit_extended_api.ExtendedAdminApi:
        """Return an administrative API for RabbitMQ.

        :username: Username to access RMQ API
                   (defaults to generated operator user)
        :password: Password to access RMQ API
                   (defaults to generated operator password)
        :returns: The RabbitMQ administrative API object
        """
        username = username or self._operator_user
        password = password or self._operator_password
        return rabbit_extended_api.ExtendedAdminApi(
            url=self._rabbitmq_mgmt_url, auth=(username, password)
        )

    def _initialize_operator_user(self) -> None:
        """Initialize the operator administrative user.

        By default, the RabbitMQ admin interface has an administravie user
        'guest' with password 'guest'. We are exposing the admin interface
        so we must create a new administravie user and remove the guest
        user.

        Create the 'operator' administravie user, grant it permissions and
        tell the peer relation this is done.

        Burn the bridge behind us and remove the guest user.
        """
        logging.info("Initializing the operator user.")
        # Use guest to create operator user
        api = self._get_admin_api("guest", "guest")
        api.create_user(
            self._operator_user,
            self._operator_password,
            tags=["administrator"],
        )
        api.create_user_permission(self._operator_user, vhost="/")
        self.peers.set_operator_user_created(self._operator_user)
        # Burn the bridge behind us.
        # We do not want to leave a known user/pass available
        logging.warning("Deleting the guest user.")
        api.delete_user("guest")

    def _set_ownership_on_data_dir(self) -> None:
        """Set ownership on /var/lib/rabbitmq."""
        container = self.unit.get_container(RABBITMQ_CONTAINER)
        paths = container.list_files(RABBITMQ_DATA_DIR, itself=True)
        if len(paths) == 0:
            return

        logger.debug(
            f"rabbitmq lib directory ownership: {paths[0].user}:{paths[0].group}"
        )
        if paths[0].user != RABBITMQ_USER or paths[0].group != RABBITMQ_GROUP:
            logger.debug(
                f"Changing ownership to {RABBITMQ_USER}:{RABBITMQ_GROUP}"
            )
            try:
                container.exec(
                    [
                        "chown",
                        "-R",
                        f"{RABBITMQ_USER}:{RABBITMQ_GROUP}",
                        RABBITMQ_DATA_DIR,
                    ]
                )
            except ExecError as e:
                logger.error(
                    f"Exited with code {e.exit_code}. Stderr:\n{e.stderr}"
                )

    def _render_and_push_config_files(self) -> None:
        """Render and push configuration files.

        Allow calling one utility function to render and push all required
        files. Calls specific render and push methods.
        """
        self._render_and_push_enabled_plugins()
        self._render_and_push_rabbitmq_conf()
        self._render_and_push_rabbitmq_env()

    def _render_and_push_enabled_plugins(self) -> None:
        """Render and push enabled plugins config.

        Render enabled plugins and push to the workload container.
        """
        container = self.unit.get_container(RABBITMQ_CONTAINER)
        enabled_plugins = ",".join(self._stored.enabled_plugins)
        enabled_plugins_template = f"[{enabled_plugins}]."
        logger.info("Pushing new enabled_plugins")
        container.push(
            "/etc/rabbitmq/enabled_plugins",
            enabled_plugins_template,
            make_dirs=True,
        )

    def _render_and_push_rabbitmq_conf(self) -> None:
        """Render and push rabbitmq conf.

        Render rabbitmq conf and push to the workload container.
        """
        container = self.unit.get_container(RABBITMQ_CONTAINER)
        loopback_users = "none"
        rabbitmq_conf = f"""
# allowing remote connections for default user is highly discouraged
# as it dramatically decreases the security of the system. Delete the user
# instead and create a new one with generated secure credentials.
loopback_users = {loopback_users}

# enable k8s clustering
cluster_formation.peer_discovery_backend = k8s

# SSL access to K8S API
cluster_formation.k8s.host = kubernetes.default.svc.cluster.local
cluster_formation.k8s.port = 443
cluster_formation.k8s.scheme = https
cluster_formation.k8s.service_name = {self.app.name}-endpoints
cluster_formation.k8s.address_type = hostname
cluster_formation.k8s.hostname_suffix = .{self.app.name}-endpoints

# Cluster cleanup and autoheal
cluster_formation.node_cleanup.interval = 30
cluster_formation.node_cleanup.only_log_warning = true
cluster_partition_handling = autoheal

queue_master_locator = min-masters

# Log to console for pod logging
log.console = true
log.file = false
"""
        logger.info("Pushing new rabbitmq.conf")
        container.push(
            "/etc/rabbitmq/rabbitmq.conf", rabbitmq_conf, make_dirs=True
        )

    def _render_and_push_pebble_notifier(self) -> bool:
        """Render notifier script and push to workload container."""
        auto_ha_frequency = int(self.config["auto-ha-frequency"])
        if auto_ha_frequency < 1:
            msg = "auto-ha-frequency must be greater than 0"
            logger.error(msg)
            raise RabbitOperatorError(msg)
        container = self.unit.get_container(RABBITMQ_CONTAINER)
        notifier = textwrap.dedent(
            f"""#!/bin/bash
            while true; do
                echo "Next event at $(date -d '+{auto_ha_frequency} minutes')"
                sleep {auto_ha_frequency * 60}
                echo "Notifying operator of timer event"
                /charm/bin/pebble notify {TIMER_NOTICE}
            done
            """
        )
        try:
            with container.pull("/usr/bin/notifier") as stream:
                content = stream.read()
        except PathError:
            content = None
        if content == notifier:
            logger.debug("Notifier script unchanged, skipping push")
            return False
        logger.info("Pushing new notifier script")
        container.push(
            "/usr/bin/notifier", notifier, make_dirs=True, permissions=0o755
        )
        return True

    def generate_nodename(self, unit_name) -> str:
        """K8S DNS nodename for local unit."""
        return (
            f"rabbit@{unit_name.replace('/', '-')}.{self.app.name}-endpoints"
        )

    @property
    def nodename(self) -> str:
        """K8S DNS nodename for local unit."""
        return self.generate_nodename(self.unit.name)

    def _render_and_push_rabbitmq_env(self) -> None:
        """Render and push rabbitmq-env conf.

        Render rabbitmq-env conf and push to the workload container.
        """
        container = self.unit.get_container(RABBITMQ_CONTAINER)
        rabbitmq_env = f"""
# Sane configuration defaults for running under K8S
NODENAME={self.nodename}
USE_LONGNAME=true
"""
        logger.info("Pushing new rabbitmq-env.conf")
        container.push("/etc/rabbitmq/rabbitmq-env.conf", rabbitmq_env)

    def _on_get_operator_info_action(self, event) -> None:
        """Action to get operator user and password information.

        Set event results with operator user and password for accessing the
        administrative web interface.
        """
        data = {
            "operator-user": self._operator_user,
            "operator-password": self._operator_password,
        }
        event.set_results(data)

    def _manage_queues(self) -> bool:
        """Whether the charm should manage queue membership."""
        return bool(self.config.get("minimum-replicas"))

    def min_replicas(self) -> int:
        """The minimum number of replicas a queue should have."""
        return self.config.get("minimum-replicas")

    def get_undersized_queues(self) -> List[dict]:
        """Return a list of queues which have fewer members than minimum."""
        api = self._get_admin_api()
        undersized_queues = [
            q
            for q in api.list_quorum_queues()
            if len(q["members"]) < self.min_replicas()
        ]
        return undersized_queues

    def _on_update_status(self, event) -> None:
        """Update status.

        Determine the state of the charm and set workload status.
        """
        if not self.peers.operator_user_created:
            self.unit.status = WaitingStatus(
                "Waiting for leader to create operator user"
            )
            return

        if not self.peers.erlang_cookie:
            self.unit.status = WaitingStatus(
                "Waiting for leader to provide erlang cookie"
            )
            return

        if not self.rabbit_running:
            self.unit.status = BlockedStatus("RabbitMQ not running")
            return

        if self._stored.rabbitmq_version:
            self.unit.set_workload_version(self._stored.rabbitmq_version)

        if self.unit.is_leader() and self._manage_queues():
            undersized_queues = self.get_undersized_queues()
            if undersized_queues:
                self.unit.status = ActiveStatus(
                    f"WARNING: {len(undersized_queues)} Queue(s) with insufficient members"
                )
                return

        self.unit.status = ActiveStatus()

    def create_amqp_credentials(
        self, event: EventBase, username: str, vhost: str
    ) -> None:
        """Set AMQP Credentials.

        :param event: The current event
        :param username: The requested username
        :param vhost: The requested vhost
        """
        # TODO TLS Support. Existing interfaces set ssl_port and ssl_ca
        logging.debug("Setting amqp connection information.")

        # Need to have the peers binding address to be available. Rabbit
        # units will need this in order to properly start and peer, so make
        # sure this is available before attempting to create any credentials
        if self.peers_bind_address is None:
            logger.debug("Waiting for peers bind address")
            event.defer()
            return

        # NOTE: fast exit if credentials are already on the relation
        if event.relation.data[self.app].get("password"):
            logging.debug(f"Credentials already provided for {username}")
            return
        try:
            if not self.does_vhost_exist(vhost):
                self.create_vhost(vhost)
            if not self.does_user_exist(username):
                password = self.create_user(username)
                self.peers.store_password(username, password)
            password = self.peers.retrieve_password(username)
            self.set_user_permissions(username, vhost)
            event.relation.data[self.app]["password"] = password
            event.relation.data[self.app]["hostname"] = self.hostname
        except requests.exceptions.HTTPError as http_e:
            # If the peers binding address exists, but the operator has not
            # been set up yet, the command will fail with a http 401 error and
            # unauthorized in the message. Just check the status code for now.
            if http_e.response.status_code == 401:
                logger.warning(
                    f"Rabbitmq has not been fully configured yet, deferring. "
                    f"Errno: {http_e.response.status_code}"
                )
                event.defer()
            else:
                raise ()
        except requests.exceptions.ConnectionError as e:
            logging.warning(
                f"Rabbitmq is not ready, deferring. Errno: {e.errno}"
            )
            event.defer()

    def _get_service_account(self, event: ActionEvent) -> None:
        """Get/create service account details for access to RabbitMQ.

        :param event: The current event
        """
        if not self.rabbit_running and not self.peers_bind_address:
            msg = "RabbitMQ not running, unable to create account"
            logger.error(msg)
            event.fail(msg)
            return

        username = event.params["username"]
        vhost = event.params["vhost"]

        try:
            if not self.does_vhost_exist(vhost):
                self.create_vhost(vhost)
            if not self.does_user_exist(username):
                password = self.create_user(username)
                self.peers.store_password(username, password)
            password = self.peers.retrieve_password(username)
            self.set_user_permissions(username, vhost)

            event.set_results(
                {
                    "username": username,
                    "password": password,
                    "vhost": vhost,
                    "ingress-address": self.ingress_address,
                    "port": RABBITMQ_SERVICE_PORT,
                    "url": self.rabbitmq_url(username, password, vhost),
                }
            )
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
        ) as e:
            msg = f"Rabbitmq is not ready. Errno: {e.errno}"
            logging.error(msg)
            event.fail(msg)

    def _add_members_to_undersized_queues(
        self,
        api: rabbit_extended_api.ExtendedAdminApi,
        nodes: list[str],
        undersized_queues: List[dict],
        replicas: int,
        dry_run: bool,
    ) -> List[str]:
        """Add members to undersized queues.

        Simple algorithm to select nodes with fewest queues to add replicas on.
        """
        queues = api.list_quorum_queues()

        nodes_queues_count = collections.Counter({node: 0 for node in nodes})
        # Get a count of how many queues each node is a member of
        for queue in queues:
            for member in queue["members"]:
                nodes_queues_count[member] += 1

        replicated_queues = []
        for queue in undersized_queues:
            needed_replicas = replicas - len(queue["members"])
            # select node with fewest queues
            sorted_count = nodes_queues_count.most_common()
            node_candidates = []
            for node, _ in reversed(sorted_count):
                if node not in queue["members"]:
                    node_candidates.append(node)
                if len(node_candidates) >= needed_replicas:
                    break
            if len(node_candidates) < needed_replicas:
                logger.warning(
                    "Not enough nodes found to replicate queue %s to HA,"
                    " availables nodes: %s, needed nodes %s",
                    queue["name"],
                    len(node_candidates),
                    needed_replicas,
                )
            logger.debug(
                "Replicating queue %r to nodes %s, dry_run=%s",
                queue["name"],
                ", ".join(node_candidates),
                dry_run,
            )
            if not dry_run:
                for node in node_candidates:
                    api.add_member(node, queue["vhost"], queue["name"])
                    nodes_queues_count[node] += 1
                replicated_queues.append(queue["name"])
        logger.info(
            "Replicated %s queues to ensure HA, dry_run=%s",
            len(replicated_queues),
            dry_run,
        )
        return replicated_queues

    def ensure_queue_ha(self, dry_run: bool = False) -> Dict[str, int]:
        """Ensure queue has HA.

        The role of this function is to ensure that all queues are available on
        at least the minimum number of replicas. If there is not enough nodes to
        support the minimum number of replicas, then this function will early
        exit and not replicate any queues.

        Must be called on the leader.

        :param dry_run: Whether to perform a dry run
        """
        undersized_queues = self.get_undersized_queues()
        if len(undersized_queues) < 1:
            msg = "No undersized queues found"
            logger.debug(msg)
            return {
                "undersized-queues": 0,
                "replicated-queues": 0,
            }

        api = self._get_admin_api()
        nodes = [node["name"] for node in api.list_nodes()]
        min_replicas = self.min_replicas()

        if len(nodes) < min_replicas:
            msg = (
                "Not enough nodes to ensure queue HA, availables nodes:"
                f" {len(nodes)}, needed nodes {min_replicas}"
            )
            logger.debug(msg)
            raise RabbitOperatorError(msg)

        replicated_queues = self._add_members_to_undersized_queues(
            api, nodes, undersized_queues, min_replicas, dry_run
        )

        if len(replicated_queues) > 0:
            logger.debug("Rebalancing queues")
            api.rebalance_queues()

        return {
            "undersized-queues": len(undersized_queues),
            "replicated-queues": len(replicated_queues),
        }

    def _ensure_queue_ha_action(self, event: ActionEvent) -> None:
        """Ensure queue has HA action.

        :param event: The current event
        """
        if not self.unit.is_leader():
            msg = "Not leader unit, unable to ensure queue HA"
            logger.error(msg)
            event.fail(msg)
            return

        if not self.rabbit_running:
            msg = "RabbitMQ not running, unable to ensure queue HA"
            logger.error(msg)
            event.fail(msg)
            return

        if not self.peers.operator_user_created:
            msg = "Operator user not created, unable to ensure queue HA"
            logger.error(msg)
            event.fail(msg)
            return

        if not self._manage_queues():
            msg = "Queue management is disabled, unable to ensure queue HA"
            logger.error(msg)
            event.fail(msg)
            return

        dry_run = event.params.get("dry-run", False)
        try:
            result = self.ensure_queue_ha(dry_run=dry_run)
        except RabbitOperatorError as e:
            event.fail(str(e))
            return
        event.set_results(
            {
                **result,
                "dry-run": dry_run,
            }
        )
        self._on_update_status(event)

    @property
    def _service_ports(self) -> List[ServicePort]:
        """Kubernetes service ports to be opened for this workload.

        We cannot use ops unit.open_port here because Juju will provision a ClusterIP
        but we need LoadBalancer.
        """
        amqp = ServicePort(RABBITMQ_SERVICE_PORT, name="amqp")
        management = ServicePort(RABBITMQ_MANAGEMENT_PORT, name="management")
        return [amqp, management]

    @property
    def lightkube_client(self):
        """Returns a lightkube client configured for this charm."""
        if self._lightkube_client is None:
            self._lightkube_client = Client(
                namespace=self.model.name,
                field_manager=self._lightkube_field_manager,
            )
        return self._lightkube_client

    def _get_lb_resource_manager(self):
        return KubernetesResourceManager(
            labels=create_charm_default_labels(
                self.app.name, self.model.name, scope=LB_LABEL
            ),
            resource_types={Service},
            lightkube_client=self.lightkube_client,
            logger=logger,
        )

    def _construct_lb(self) -> Service:
        return Service(
            metadata=ObjectMeta(
                name=f"{self._lb_name}",
                namespace=self.model.name,
                labels={"app.kubernetes.io/name": self.app.name},
            ),
            spec=ServiceSpec(
                ports=self._service_ports,
                selector={"app.kubernetes.io/name": self.app.name},
                type="LoadBalancer",
            ),
        )

    def _reconcile_lb(self, _):
        """Reconcile the LoadBalancer's state."""
        if not self.unit.is_leader():
            return

        klm = self._get_lb_resource_manager()
        resources_list = []
        resources_list.append(self._construct_lb())
        logger.info(
            f"Patching k8s loadbalancer service object {self._lb_name}"
        )
        klm.reconcile(resources_list)

    def _on_remove(self, _):
        if not self.unit.is_leader():
            return

        logger.info(
            f"Removing k8s loadbalancer service object {self._lb_name}"
        )
        klm = self._get_lb_resource_manager()
        klm.delete()


if __name__ == "__main__":
    ops.main(RabbitMQOperatorCharm)
