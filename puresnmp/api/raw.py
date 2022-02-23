"""
This module contains a high-level API to SNMP functions.

The arguments and return values of these functions have types which are
internal to ``puresnmp`` (subclasses of :py:class:`x690.types.Type`).

Alternatively, there is :py:mod:`puresnmp.api.pythonic` which converts
these values into pure Python types. This makes day-to-day programming a bit
easier but loses type information which may be useful in some edge-cases.
"""

import asyncio
import logging
from asyncio import get_event_loop
from asyncio.events import AbstractEventLoop
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from ipaddress import ip_address
from typing import (
    Any,
    AsyncGenerator,
    Callable,
    Dict,
    Generator,
    List,
    Optional,
    Set,
    Tuple,
)
from typing import Type as TType
from typing import TypeVar, cast

from typing_extensions import Protocol
from x690.types import Integer, Null, ObjectIdentifier, Sequence, Type

import puresnmp.plugins.mpm as mpm
from puresnmp.typevars import SocketResponse, TAnyIp

from ..const import DEFAULT_RETRIES, DEFAULT_TIMEOUT, ERRORS_STRICT, ERRORS_WARN
from ..credentials import V2C, Credentials
from ..exc import FaultySNMPImplementation, NoSuchOID, SnmpError
from ..pdu import (
    PDU,
    BulkGetRequest,
    EndOfMibView,
    GetNextRequest,
    GetRequest,
    NoSuchInstance,
    NoSuchObject,
    PDUContent,
    SetRequest,
    Trap,
)
from ..transport import Endpoint, TSender, listen, send_udp
from ..util import (
    BulkResult,
    get_request_id,
    get_unfinished_walk_oids,
    group_varbinds,
    tablify,
    validate_response_id,
)
from ..varbind import VarBind

TWalkResponse = AsyncGenerator[VarBind, None]
T = TypeVar("T", bound=TType[Any])  # pylint: disable=invalid-name

LOG = logging.getLogger(__name__)


class TFetcher(Protocol):
    """
    Protocol for a callable that is responsible to fetch a collection of OIDs
    from the remote device
    """

    # pylint: disable=too-few-public-methods, no-self-access

    async def __call__(
        self, oids: List[ObjectIdentifier]
    ) -> List["VarBind"]:  # pragma: no cover
        ...


def deduped_varbinds(
    requested_oids: List[ObjectIdentifier],
    grouped_oids: Dict[ObjectIdentifier, List[VarBind]],
    yielded: Set[ObjectIdentifier],
) -> Generator[VarBind, None, None]:
    """
    Generate grouped OIDs by ensuring they are contained in the original
    request and have no duplicates.

    >>> OID = ObjectIdentifier
    >>> list(deduped_varbinds(
    ...     [OID("1.2"), OID("2.3")],
    ...     {
    ...         OID("1.2"): [VarBind(OID("1.2.3.4"), 1)],
    ...         OID("1.2"): [VarBind(OID("1.2.3.4"), 1)],
    ...         OID("5.6"): [VarBind(OID("5.6.7.8"), 1)],
    ...     },
    ...     set()
    ... ))
    [VarBind(oid=ObjectIdentifier('1.2.3.4'), value=1)]

    :param requested_oids: A list of OIDs which were originally requested. If
        any value from the grouped varbinds are not children of any of these
        OIDs, the issue is logged and the value is skipped.
    :param grouped_oids: The OIDs that need to be verified & deduped.
    :param yielded: A set containing all OIDs that have already been
        generated by this function. This set will be updated by this function
        whenever a value is returned to detect duplicates.
    """
    for var in sorted(grouped_oids.values()):
        for varbind in var:
            containment = [varbind.oid in _ for _ in requested_oids]
            if not any(containment) or varbind.oid in yielded:
                LOG.debug(
                    "Unexpected device response: Returned VarBind %s "
                    "was either not contained in the requested tree or "
                    "appeared more than once. Skipping!",
                    varbind,
                )
                continue
            yielded.add(varbind.oid)
            yield varbind


@dataclass(frozen=True)
class Context:
    """
    Information about the current SNMP context
    """

    engine_id: bytes
    name: bytes


@dataclass(frozen=True)
class ClientConfig:
    """
    Overridable configuration for SNMP clients

    These settings can be overridden via :py:meth:`Client.reconfigure`
    """

    #: The credentials used to apply to SNMP requests
    credentials: Credentials
    #: The SNMPv3 Context. For SNMPv1 or SNMPv2 this value is ignored
    context: Context
    #: The SNMPv3 "local config cache". For SNMPv1 or SNMPv2 this value is
    #: ignored
    lcd: Dict[str, Any]
    #: The socket timeout for network requests. This value is passed through
    #: to the client's "sender" callable
    timeout: int = DEFAULT_TIMEOUT
    #: The number of retries we attempt when sending packets to the remote
    #: device before giving up. Note that some devices may refuse connections
    #: attempts if too many requests were made with incorrect credentials.
    #: Defaults to 10 retries
    retries: int = DEFAULT_RETRIES


class Client:
    """
    A client to execute SNMP commands on a remote device.

    To run SNMP commands on a remote device, create an instance for that
    device, and then call the instance methods.

    All functions are based on asyncio and must be used in an async context.

    Credentials need to be instances of classes taken from
    :py:mod:`puresnmp.credentials` which are used to determine the
    appropriate communication model for this client instance.

    >>> from puresnmp import Client, ObjectIdentifier, V2C
    >>> import warnings
    >>> warnings.simplefilter("ignore")
    >>> client = Client("192.0.2.1", V2C("public"))
    >>> client.get(ObjectIdentifier("1.3.6.1.2.1.1.2.0"))  # doctest: +ELLIPSIS
    <coroutine ...>

    :param ip: The IP-address of the remote SNMP device
    :param credentials: User credentials for the request. These define the
        underlying protocol in use. See :py:mod:`puresnmp.credentials` for
        possible types.
    :param port: The UDP port for the remote device
    :param sender: A callable responsible to send out data to the remote
        device. The default implementation will use UDP using the IP and port
        given in the other arguments.
    :param context_name: An optional context for SNMPv3 requests
    :param engine_id: An optional Engine ID for SNMPv3 requests. Helper
        functions are provided in :py:mod:`puresnmp.util` to generate valid IDs.
    """

    def __init__(
        self,
        ip: str,
        credentials: Credentials,
        port: int = 161,
        sender: TSender = send_udp,
        context_name: bytes = b"",
        engine_id: bytes = b"",
    ) -> None:

        lcd: Dict[str, Any] = {}
        self.config = ClientConfig(
            credentials=credentials,
            context=Context(engine_id, context_name),
            lcd=lcd,
        )

        endpoint = Endpoint(ip_address(ip), port)

        async def handler(data: bytes) -> bytes:  # pragma: no cover
            """
            A callable that is bound to a given IP/Port combination, capable of
            sending raw bytes to that endpoint. This is passed to
            message-processing models in case they need to communicate with the
            device.

            At the time of this writing, this is only required vor SNMPv3
            discovery messages.
            """
            return await sender(
                endpoint,
                data,
                timeout=self.config.timeout,
                retries=self.config.retries,
            )

        self.sender = sender
        self.transport_handler = handler
        self.endpoint = endpoint
        self.mpm = mpm.create(credentials.mpm, handler, lcd)

    @property
    def credentials(self) -> Credentials:
        """
        Accessor to the client credentials
        """
        return self.config.credentials

    @property
    def context(self) -> Context:
        """
        Accessor to the SNMPv3 context
        """
        return self.config.context

    @property
    def ip(self) -> TAnyIp:
        """
        Accessor to the endpoint IP address
        """
        return self.endpoint.ip

    @property
    def port(self) -> int:
        """
        Accessor to the endpoint port
        """
        return self.endpoint.port

    @contextmanager
    def reconfigure(self, **kwargs: Any) -> Generator[None, None, None]:
        """
        Temporarily reconfigure the client.

        Some values may need to be modified during the lifetime of the client
        for some requests. A typical example would be using different
        credentials for "set" commands, or different socket timeouts for some
        targeted requests.

        This is provided via this context-manager. When the context-manager
        exits, the previous config is restored

        The values that can be overridden delegate to
        :py:class:`~.ClientConfig`. Any fields in that class can be overridden

        >>> client = Client("192.0.2.1", V2C("public"))
        >>> client.config.timeout
        6
        >>> with client.reconfigure(timeout=10):
        ...     client.config.timeout
        10
        >>> client.config.timeout
        6
        """
        old_config = self.config
        old_mpm = self.mpm
        new_config = replace(old_config, **kwargs)

        try:
            if "credentials" in kwargs and type(
                self.config.credentials
            ) != type(kwargs["credentials"]):
                # New credentials may switch from one SNMP version to another
                # so we need to create a new message-processing-model
                lcd: Dict[str, Any] = {}
                self.mpm = mpm.create(
                    kwargs["credentials"].mpm, self.transport_handler, lcd
                )
            self.config = new_config
            yield
        finally:
            self.config = old_config
            self.mpm = old_mpm

    async def _send(self, pdu: PDU, request_id: int) -> PDU:
        packet, _ = await self.mpm.encode(
            request_id,
            self.credentials,
            self.context.engine_id,
            self.context.name,
            pdu,
        )
        raw_response = await self.sender(
            self.endpoint,
            bytes(packet),
            timeout=self.config.timeout,
            retries=self.config.retries,
        )
        response = self.mpm.decode(raw_response, self.credentials)
        validate_response_id(request_id, response.value.request_id)
        return response

    async def get(self, oid: ObjectIdentifier) -> Type[Any]:
        """
        Retrieve the value of a single OID

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> from puresnmp.util import sync
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> client = Client("127.0.0.1", V2C("private"), port=50009)
        >>> coro = client.get(OID("1.3.6.1.2.1.1.2.0"))
        >>> sync(coro)  # doctest: +SKIP
        ObjectIdentifier('1.3.6.1.4.1.8072.3.2.10')
        """
        result = await self.multiget([oid])
        if isinstance(result[0], (NoSuchObject, NoSuchInstance)):
            raise NoSuchOID(oid)
        return result[0]

    async def multiget(self, oids: List[ObjectIdentifier]) -> List[Type[Any]]:
        """
        Retrieve (scalar) values from multiple OIDs in one request.

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> from puresnmp.util import sync
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> client = Client("127.0.0.1", V2C("private"), port=50009)
        >>> coro = client.multiget(
        ...     [OID('1.3.6.1.2.1.1.2.0'), OID('1.3.6.1.2.1.1.1.0')]
        ... )
        >>> sync(coro)  # doctest: +SKIP
        [ObjectIdentifier('1.3.6.1.4.1.8072.3.2.10'), OctetString(b'Linux c8582f39c32b 4.15.0-115-generic #116-Ubuntu SMP Wed Aug 26 14:04:49 UTC 2020 x86_64')]
        """

        parsed_oids = [VarBind(oid, Null()) for oid in oids]

        request_id = get_request_id()
        pdu = GetRequest(PDUContent(request_id, parsed_oids))
        response = await self._send(pdu, request_id)
        output = [value for _, value in response.value.varbinds]
        if len(output) != len(oids):
            raise SnmpError(
                "Unexpected response. Expected %d varbind, "
                "but got %d!" % (len(oids), len(output))
            )
        return output

    async def getnext(self, oid: ObjectIdentifier) -> VarBind:
        """
        Executes a single SNMP GETNEXT request (used inside *walk*).

        >>> from puresnmp import Client, ObjectIdentifier as OID
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> client = Client("192.0.2.1", V2C("private"))
        >>> # The line below needs to be "awaited" to get the result.
        >>> # This is not shown here to make it work with doctest
        >>> client.getnext(OID('1.2.3.4'))
        <coroutine object ...>
        """
        result = await self.multigetnext([oid])
        if isinstance(result[0], (NoSuchObject, NoSuchInstance)):
            raise NoSuchOID(oid)
        return result[0]

    async def walk(
        self,
        oid: ObjectIdentifier,
        errors: str = ERRORS_STRICT,
    ) -> TWalkResponse:
        """
        A convenience method delegating to :py:meth:`~.multiwalk` with
        exactly one OID
        """
        async for row in self.multiwalk([oid], errors=errors):
            yield row

    async def multiwalk(
        self,
        oids: List[ObjectIdentifier],
        fetcher: Optional[TFetcher] = None,
        errors: str = ERRORS_STRICT,
    ) -> TWalkResponse:
        """
        Retrieve all values "below" multiple OIDs with a single operation.

        Note: This will send out as many "GetNext" requests as needed.

        This is almost the same as :py:meth:`~.walk` except that it is
        capable of iterating over multiple OIDs at the same time.

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> from puresnmp.util import sync
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> async def example():
        ...     client = Client("127.0.0.1", V2C("private"), port=50009)
        ...     result = client.multiwalk(
        ...         [OID('1.3.6.1.2.1.1'), OID('1.3.6.1.4.1.1')]
        ...     )
        ...     output = []
        ...     async for row in result:
        ...         output.append(row)
        ...     return output
        >>> sync(example())  # doctest: +SKIP
        [VarBind(oid=ObjectIdentifier('1.3.6.1.2.1.1.1.0'), value=Oct...]
        """
        if fetcher is None:
            fetcher = self.multigetnext

        LOG.debug("Walking on %d OIDs using %s", len(oids), fetcher.__name__)
        varbinds = await fetcher(oids)
        grouped_oids = group_varbinds(varbinds, oids)
        unfinished_oids = get_unfinished_walk_oids(grouped_oids)
        yielded: Set[ObjectIdentifier] = set()
        for varbind in deduped_varbinds(oids, grouped_oids, yielded):
            yield varbind

        # As long as we have unfinished OIDs, we need to continue the walk for
        # those.
        while unfinished_oids:
            next_fetches = [_[1].value.oid for _ in unfinished_oids]
            try:
                varbinds = await fetcher(next_fetches)
            except NoSuchOID:
                # Reached end of OID tree, finish iteration
                break
            except FaultySNMPImplementation as exc:
                if errors == ERRORS_WARN:
                    LOG.warning(
                        "SNMP walk aborted prematurely due to faulty SNMP "
                        "implementation on device %r! Upon running a "
                        "GetNext on OIDs %r it returned the following "
                        "error: %s",
                        self.endpoint,
                        next_fetches,
                        exc,
                    )
                    break
                raise
            grouped_oids = group_varbinds(
                varbinds, next_fetches, user_roots=oids
            )
            unfinished_oids = get_unfinished_walk_oids(grouped_oids)
            if LOG.isEnabledFor(logging.DEBUG) and len(oids) > 1:
                LOG.debug(
                    "%d of %d OIDs need to be continued",
                    len(unfinished_oids),
                    len(oids),
                )
            for varbind in deduped_varbinds(oids, grouped_oids, yielded):
                yield varbind

    async def multigetnext(self, oids: List[ObjectIdentifier]) -> List[VarBind]:
        """
        Executes a single multi-oid GETNEXT request.

        The request sends one packet to the remote host requesting the value
        of the OIDs following one or more given OIDs.

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> from puresnmp.util import sync
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> client = Client("127.0.0.1", V2C("private"), port=50009)
        >>> # The line below needs to be "awaited" to get the result.
        >>> # This is not shown here to make it work with doctest
        >>> coro = client.multigetnext(
        ...     [OID('1.3.6.1.2.1.1.2.0'), OID('1.3.6.1.2.1.1.1.0')]
        ... )
        >>> sync(coro)  # doctest: +ELLIPSIS +SKIP
        [VarBind(oid=ObjectIdentifier('1.3.6.1.2.1.1.3.0'), value=TimeTicks(...)), VarBind(oid=ObjectIdentifier('1.3.6.1.2.1.1.2.0'), value=ObjectIdentifier('1.3.6.1.4.1.8072.3.2.10'))]
        """
        varbinds = [VarBind(oid, Null()) for oid in oids]
        request_id = get_request_id()
        pdu = GetNextRequest(PDUContent(request_id, varbinds))
        response_object = await self._send(pdu, request_id)
        if len(response_object.value.varbinds) != len(oids):
            raise SnmpError(
                "Invalid response! Expected exactly %d varbind, "
                "but got %d" % (len(oids), len(response_object.value.varbinds))
            )

        output = []
        for oid, value in response_object.value.varbinds:
            if isinstance(value, EndOfMibView):
                break
            output.append(VarBind(oid, value))

        # Verify that the OIDs we retrieved are successors of the requested OIDs
        for requested, retrieved in zip(oids, output):
            if not requested < retrieved.oid:
                raise FaultySNMPImplementation(
                    "The OID %s is not a successor of %s!"
                    % (retrieved.oid, requested)
                )
        return output

    async def table(self, oid: ObjectIdentifier) -> List[Dict[str, Any]]:
        """
        Fetch an SNMP table

        The resulting output will be a list of dictionaries where each
        dictionary corresponds to a row of the table.

        SNMP Tables are indexed as follows::

            <base-oid>.<column-id>.<row-id>

        A "row-id" can be either a single numerical value, or a partial OID.
        The row-id will be contained in key ``'0'`` of each row (as a string)
        representing that partial OID (often a suffix which can be used in
        other tables). This key ``'0'`` is automatically injected by
        ``puresnmp``. This ensures that the row-index is available even for
        tables that don't include that value themselves.

        SNMP-Tables are fetched first by column, then by row (by the nature
        of the defined MIB structure). This means that this method has to
        consume the complete table before being able to return anything.

        Example output:

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> from puresnmp.util import sync
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> client = Client("127.0.0.1", V2C("private"), port=50009)
        >>> coro = client.table(OID("1.3.6.1.2.1.2.2.1"))
        >>> sync(coro)  # doctest: +SKIP
        [{'0': '1', '1': Integer(1), ... '22': ObjectIdentifier('0.0')}]
        """
        tmp = []
        varbinds = self.walk(oid)
        async for varbind in varbinds:
            tmp.append(varbind)
        as_table = tablify(tmp, num_base_nodes=len(oid))
        return as_table

    async def set(
        self,
        oid: ObjectIdentifier,
        value: T,
    ) -> T:
        """
        Update a value on the remote host

        Values must be a subclass of :py:class:`x690.types.Type`. See
        :py:mod:`x690.types` for a predefined collection of types.

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> from puresnmp.util import sync
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> from x690.types import OctetString
        >>> client = Client("127.0.0.1", V2C("private"), port=50009)
        >>> coro = client.set(
        ...     OID("1.3.6.1.2.1.1.4.0"), OctetString(b'new contact value')
        ... )
        >>> sync(coro)  # doctest: +SKIP
        OctetString(b'new contact value')
        """
        value_internal = cast(Type[Any], value)
        result = await self.multiset({oid: value_internal})
        return result[oid]  # type: ignore

    async def multiset(
        self, mappings: Dict[ObjectIdentifier, Type[Any]]
    ) -> Dict[ObjectIdentifier, Type[Any]]:
        """
        Executes an SNMP SET request on multiple OIDs. The result is returned as
        pure Python data structure.

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> from puresnmp.util import sync
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> from x690.types import OctetString
        >>> client = Client("127.0.0.1", V2C("private"), port=50009)
        >>> coro = client.multiset({
        ...     OID('1.3.6.1.2.1.1.4.0'): OctetString(b'new-contact'),
        ...     OID('1.3.6.1.2.1.1.6.0'): OctetString(b'new-location')
        ... })
        >>> sync(coro)  # doctest: +ELLIPSIS +SKIP
        {ObjectIdentifier('1.3.6.1.2.1.1.4.0'): OctetString(b'new-c...cation')}
        """

        if any([not isinstance(v, Type) for v in mappings.values()]):
            raise TypeError(
                "SNMP requires typing information. The value for a "
                '"set" request must be an instance of "Type"!'
            )

        binds = [VarBind(oid, value) for oid, value in mappings.items()]

        pdu = SetRequest(PDUContent(get_request_id(), binds))
        response = await self._send(pdu, get_request_id())

        output = {oid: value for oid, value in response.value.varbinds}
        if len(output) != len(mappings):
            raise SnmpError(
                "Unexpected response. Expected %d varbinds, "
                "but got %d!" % (len(mappings), len(output))
            )
        return output

    async def bulkget(
        self,
        scalar_oids: List[ObjectIdentifier],
        repeating_oids: List[ObjectIdentifier],
        max_list_size: int = 1,
    ) -> BulkResult:
        # pylint: disable=unused-argument, too-many-locals
        """
        Runs a "bulk" get operation and returns a :py:class:`~.BulkResult`
        instance. This contains both a mapping for the scalar variables (the
        "non-repeaters") and an OrderedDict instance containing the remaining
        list (the "repeaters").

        The OrderedDict is ordered the same way as the SNMP response
        (whatever the remote device returns).

        This operation can retrieve both single/scalar values *and* lists of
        values ("repeating values") in one single request. You can for
        example retrieve the hostname (a scalar value), the list of
        interfaces (a repeating value) and the list of physical entities
        (another repeating value) in one single request.

        Note that this behaves like a **getnext** request for scalar values!
        So you will receive the value of the OID which is *immediately
        following* the OID you specified for both scalar and repeating
        values!

        :param scalar_oids: contains the OIDs that should be fetched as single
            value.
        :param repeating_oids: contains the OIDs that should be fetched as list.
        :param max_list_size: defines the max length of each list.

        >>> from puresnmp import Client, ObjectIdentifier as OID, V2C
        >>> import warnings
        >>> warnings.simplefilter("ignore")
        >>> client = Client("192.0.2.1", V2C("private"), port=50009)
        >>> result = client.bulkget(  # doctest: +SKIP
        ...     scalar_oids=[
        ...         OID('1.3.6.1.2.1.1.1'),
        ...         OID('1.3.6.1.2.1.1.2'),
        ...     ],
        ...     repeating_oids=[
        ...         OID('1.3.6.1.2.1.3.1'),
        ...         OID('1.3.6.1.2.1.5.1'),
        ...     ],
        ...     max_list_size=10
        ... )
        BulkResult(
            scalars={
                ObjectIdentifier('1.3.6.1.2.1.1.1.0'): OctetString(
                    b'Linux c8582f39c32b 4.15.0-115-generic #116-Ubuntu SMP '
                    b'Wed Aug 26 14:04:49 UTC 2020 x86_64'
                ),
                ObjectIdentifier('1.3.6.1.2.1.1.2.0'): ObjectIdentifier(
                    '1.3.6.1.4.1.8072.3.2.10'
                )
            },
            listing=OrderedDict([
                (
                    ObjectIdentifier('1.3.6.1.2.1.3.1.1.1.8769.1.10.100.0.1'),
                    Integer(8769),
                ),
                (ObjectIdentifier('1.3.6.1.2.1.5.1.0'), Counter(1)),
                (
                    ObjectIdentifier('1.3.6.1.2.1.3.1.1.2.8769.1.10.100.0.1'),
                    OctetString(b'\x02B\x03\x96#>'),
                ),
                (ObjectIdentifier('1.3.6.1.2.1.5.2.0'), Counter(0)),
                (
                    ObjectIdentifier('1.3.6.1.2.1.3.1.1.3.8769.1.10.100.0.1'),
                    IpAddress(IPv4Address('10.100.0.1')),
                ),
                (ObjectIdentifier('1.3.6.1.2.1.5.3.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.4.1.0'), Integer(1)),
                (ObjectIdentifier('1.3.6.1.2.1.5.4.0'), Counter(1)),
                (ObjectIdentifier('1.3.6.1.2.1.4.2.0'), Integer(64)),
                (ObjectIdentifier('1.3.6.1.2.1.5.5.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.4.3.0'), Counter(4)),
                (ObjectIdentifier('1.3.6.1.2.1.5.6.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.4.4.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.5.7.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.4.5.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.5.8.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.4.6.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.5.9.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.4.7.0'), Counter(0)),
                (ObjectIdentifier('1.3.6.1.2.1.5.10.0'), Counter(0))
            ])
        )
        """

        scalar_oids = scalar_oids or []  # protect against empty values
        repeating_oids = repeating_oids or []  # protect against empty values

        oids = list(scalar_oids) + list(repeating_oids)

        non_repeaters = len(scalar_oids)

        request_id = get_request_id()
        pdu = BulkGetRequest(request_id, non_repeaters, max_list_size, *oids)
        get_response = await self._send(pdu, request_id)

        # See RFC=3416 for details of the following calculation
        n = min(non_repeaters, len(oids))
        m = max_list_size
        r = max(len(oids) - n, 0)  # pylint: disable=invalid-name
        expected_max_varbinds = n + (m * r)

        n_retrieved_varbinds = len(get_response.value.varbinds)
        if n_retrieved_varbinds > expected_max_varbinds:
            raise SnmpError(
                "Unexpected response. Expected no more than %d "
                "varbinds, but got %d!"
                % (expected_max_varbinds, n_retrieved_varbinds)
            )

        # cut off the scalar OIDs from the listing(s)
        scalar_tmp = get_response.value.varbinds[0 : len(scalar_oids)]
        repeating_tmp = get_response.value.varbinds[len(scalar_oids) :]

        # prepare output for scalar OIDs
        scalar_out = {oid: value for oid, value in scalar_tmp}

        # prepare output for listing
        repeating_out = OrderedDict()  # type: Dict[ObjectIdentifier, Type[Any]]
        for oid, value in repeating_tmp:
            if isinstance(value, EndOfMibView):
                break
            repeating_out[oid] = value

        return BulkResult(scalar_out, repeating_out)

    def _bulkwalk_fetcher(self, bulk_size: int = 10) -> TFetcher:
        """
        Create a bulk fetcher with a fixed limit on "repeatable" OIDs.
        """

        async def fetcher(oids: List[ObjectIdentifier]) -> List[VarBind]:
            """
            Executes a SNMP BulkGet request.
            """
            result = await self.bulkget([], oids, max_list_size=bulk_size)
            return [VarBind((k), v) for k, v in result.listing.items()]

        fetcher.__name__ = "_bulkwalk_fetcher(%d)" % bulk_size
        return fetcher

    async def bulkwalk(
        self,
        oids: List[ObjectIdentifier],
        bulk_size: int = 10,
    ) -> TWalkResponse:
        """
        Identical to :py:meth:`~.walk` but uses "bulk" requests instead.

        "Bulk" requests fetch more than one OID in one request, so they are
        more efficient, but large return-values may overflow the transport
        buffer.

        :param oids: Delegated to :py:meth:`~.walk`
        :param bulk_size: Number of values to fetch per request.
        """

        if not isinstance(oids, list):
            raise TypeError("OIDS need to be passed as list!")

        result = self.multiwalk(
            oids,
            fetcher=self._bulkwalk_fetcher(bulk_size),
        )
        async for oid, value in result:
            yield VarBind(oid, value)

    async def bulktable(
        self,
        oid: ObjectIdentifier,
        bulk_size: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Identical to :py:meth:`~.table` but uses "bulk" requests.

        "Bulk" requests fetch more than one OID in one request, so they are
        more efficient, but large return-values may overflow the transport
        buffer.

        :param oid: Delegated to :py:meth:`~.table`
        :param bulk_size: Number of values to fetch per request.
        """
        tmp = []
        varbinds = self.bulkwalk([oid], bulk_size=bulk_size)
        async for varbind in varbinds:
            tmp.append(varbind)
        as_table = tablify(tmp, num_base_nodes=len(oid) + 1)
        return as_table


def register_trap_callback(
    callback: Callable[[PDU], Any],
    listen_address: str = "0.0.0.0",
    port: int = 162,
    credentials: Credentials = V2C("public"),
    loop: Optional[AbstractEventLoop] = None,
) -> AbstractEventLoop:
    """
    Registers a callback function for for SNMP traps.

    Every time a trap is received, the callback is called with the PDU
    contained in that trap.

    As per :rfc:`3416#section-4.2.6`, the first two varbinds are the system
    uptime and the trap OID. The following varbinds are the body of the trap

    The callback will be called on the current asyncio loop. Alternatively, a
    loop can be passed into this function in which case, the traps will be
    handler on that loop instead.
    """
    if loop is None:
        loop = get_event_loop()

    def decode(packet: SocketResponse) -> None:
        async def handler(data: bytes) -> bytes:
            return await send_udp(
                Endpoint(ip_address(packet.info.address), packet.info.port),
                data,
            )

        lcd: Dict[str, Any] = {}

        as_sequence = Sequence.decode(packet.data)

        obj = cast(Tuple[Integer, Integer, Trap], as_sequence[0])

        mproc = mpm.create(obj[0].value, handler, lcd)
        trap = mproc.decode(packet.data, credentials)
        asyncio.ensure_future(callback(trap))

    handler = listen(listen_address, port, decode, loop)
    loop.run_until_complete(handler)
    return loop
