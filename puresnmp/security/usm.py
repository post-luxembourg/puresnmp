from dataclasses import dataclass, replace
from textwrap import indent
from typing import Any, Awaitable, Callable, Dict

from x690.types import Integer, OctetString, Sequence, pop_tlv
from x690.util import INDENT_STRING

import puresnmp.auth as auth
import puresnmp.priv as priv
from puresnmp.adt import HeaderData, Message, ScopedPDU, V3Flags
from puresnmp.credentials import V3, Credentials
from puresnmp.exc import SnmpError
from puresnmp.pdu import GetRequest
from puresnmp.security import SecurityModel
from puresnmp.transport import MESSAGE_MAX_SIZE, get_request_id

IDENTIFIER = 3


def reset_digest(message: Message) -> Message:
    # As per https://tools.ietf.org/html/rfc3414#section-6.3.1,
    # the auth-key needs to be initialised to 12 zeroes
    secparams = USMSecurityParameters.decode(message.security_parameters)
    neutral = replace(secparams, auth_params=b"\x00" * 12)
    output = replace(
        message,
        security_parameters=bytes(neutral),
    )
    return output


class USMError(SnmpError):
    pass


class UnsupportedSecurityLevel(USMError):
    pass


@dataclass(frozen=True)
class DiscoData:
    authoritative_engine_id: bytes
    authoritative_engine_boots: int
    authoritative_engine_time: int
    unknown_engine_ids: int


@dataclass(frozen=True)
class USMSecurityParameters:
    """
    This class wraps the various values for the USM
    """

    authoritative_engine_id: bytes
    authoritative_engine_boots: int
    authoritative_engine_time: int
    user_name: bytes
    auth_params: bytes
    priv_params: bytes

    @staticmethod
    def decode(data: bytes) -> "USMSecurityParameters":
        """
        Construct a USMSecurityParameters instance from pure bytes
        """
        seq, _ = pop_tlv(data, enforce_type=Sequence)
        return USMSecurityParameters.from_snmp_type(seq)

    @staticmethod
    def from_snmp_type(seq: Sequence) -> "USMSecurityParameters":
        return USMSecurityParameters(
            authoritative_engine_id=seq[0].pythonize(),
            authoritative_engine_boots=seq[1].pythonize(),
            authoritative_engine_time=seq[2].pythonize(),
            user_name=seq[3].pythonize(),
            auth_params=seq[4].pythonize(),
            priv_params=seq[5].pythonize(),
        )

    def __bytes__(self) -> bytes:
        return bytes(self.as_snmp_type())

    def as_snmp_type(self) -> Sequence:
        return Sequence(
            OctetString(self.authoritative_engine_id),
            Integer(self.authoritative_engine_boots),
            Integer(self.authoritative_engine_time),
            OctetString(self.user_name),
            OctetString(self.auth_params),
            OctetString(self.priv_params),
        )

    def pretty(self, depth: int = 0) -> str:
        """
        Return a value for CLI display
        """
        lines = ["Security Parameters"]
        lines.extend(
            [
                f"{INDENT_STRING}Engine ID   : {self.authoritative_engine_id!r}",
                f"{INDENT_STRING}Engine Boots: {self.authoritative_engine_boots}",
                f"{INDENT_STRING}Engine Time : {self.authoritative_engine_time}",
                f"{INDENT_STRING}Username    : {self.user_name!r}",
                f"{INDENT_STRING}Auth Params : {self.auth_params!r}",
                f"{INDENT_STRING}Priv Params : {self.priv_params!r}",
            ]
        )
        return indent("\n".join(lines), INDENT_STRING * depth)


class UserSecurityModel(SecurityModel):
    def set_engine_timing(self, engine_id, boots, time):
        # TODO redundant with set_timing_values?
        engine_config = self.local_config.setdefault(engine_id, {})
        engine_config["authoritative_engine_boots"] = boots
        engine_config["authoritative_engine_time"] = time

    def set_default_auth(self, auth: Dict[bytes, Dict[str, Any]]) -> None:
        self.default_auth = auth

    def generate_request_message(
        self,
        message: Message,
        security_engine_id: bytes,
        credentials: Credentials,
    ) -> Message:
        if not isinstance(credentials, V3):
            raise TypeError(
                "Credentials must be a V3 instance for this scurity model!"
            )

        security_name = credentials.username.encode("ascii")
        engine_config = self.local_config[security_engine_id]
        engine_boots = engine_config["authoritative_engine_boots"]
        engine_time = engine_config["authoritative_engine_time"]

        if credentials.priv is not None and not all(
            [credentials.priv.method, credentials.auth.method]
        ):
            raise UnsupportedSecurityLevel(
                f"Security level needs privacy, but either auth-proto or "
                f"priv-proto are missing for user {security_name!r}"
            )

        if credentials.priv is not None:
            priv_method = priv.create(credentials.priv.method)
            key = credentials.priv.key
            try:
                from x690.types import OctetString

                encrypted, salt = priv_method.encrypt_data(
                    key,
                    security_engine_id,
                    engine_boots,
                    bytes(message.scoped_pdu),
                )
                scoped_pdu = OctetString(encrypted)
            except Exception as exc:
                # TODO Use a proper app-exception here
                raise SnmpError("EncryptionError") from exc
        else:
            scoped_pdu = message.scoped_pdu
            salt = b""

        unauthed_message = replace(
            message,
            scoped_pdu=scoped_pdu,
            security_parameters=bytes(
                USMSecurityParameters(
                    security_engine_id,
                    engine_boots,
                    engine_time,
                    security_name,
                    b"",
                    salt,
                )
            ),
        )

        if credentials.auth is not None and not credentials.auth.method:
            raise UnsupportedSecurityLevel(
                f"Security level needs authentication, but auth-proto "
                f"is missing for user {security_name!r}"
            )

        if credentials.auth is not None:
            auth_method = auth.create(credentials.auth.method)
            try:
                without_digest = reset_digest(unauthed_message)
                auth_result = auth_method.authenticate_outgoing_message(
                    credentials.auth.key,
                    bytes(without_digest),
                    security_engine_id,
                )
                security_params = USMSecurityParameters(
                    security_engine_id,
                    engine_boots,
                    engine_time,
                    security_name,
                    auth_result,
                    salt,
                )
                authed_message = Message(
                    unauthed_message.version,
                    unauthed_message.global_data,
                    bytes(security_params),
                    unauthed_message.scoped_pdu,
                )
                return authed_message  # XXX return misplaced
            except Exception as exc:
                # TODO improve error message
                raise SnmpError("authenticationFailure") from exc
        else:
            auth_params = b""

        security_params = USMSecurityParameters(
            authoritative_engine_id=security_engine_id,
            authoritative_engine_boots=engine_boots,
            authoritative_engine_time=engine_time,
            user_name=security_name,
            auth_params=auth_params,
            priv_params=salt,
        )

        secured_message = Message(
            message.version,
            message.global_data,
            bytes(security_params),
            scoped_pdu,
        )
        return secured_message

    def process_incoming_message(
        self, message: Message, credentials: Credentials
    ) -> Message:
        # TODO: Validate engine-id.
        # TODO: Validate incoming username against the request

        security_params = USMSecurityParameters.decode(
            message.security_parameters
        )

        security_name = security_params.user_name
        if security_name != credentials.username.encode("ascii"):
            # See https://tools.ietf.org/html/rfc3414#section-3.1
            # TODO better exception class
            raise SnmpError(f"Unknown User {security_name!r}")

        auth_method = auth.create(credentials.auth.method)

        if message.global_data.flags.auth and not auth_method:
            raise UnsupportedSecurityLevel(
                f"Security level needs authentication, but auth-proto "
                f"is missing for user {security_name!r}"
            )
        if message.global_data.flags.auth:
            try:
                without_digest = reset_digest(message)
                auth_method.authenticate_incoming_message(
                    credentials.auth.key,
                    bytes(without_digest),
                    security_params.auth_params,
                    security_params.authoritative_engine_id,
                )
            except Exception as exc:
                # TODO improve error message
                raise SnmpError("authenticationFailure") from exc

        if message.global_data.flags.priv:
            priv_method = priv.create(credentials.priv.method)
            key = credentials.priv.key
            try:
                if not isinstance(message.scoped_pdu, OctetString):
                    raise SnmpError(
                        "Unexpectedly received unencrypted PDU with a security level requesting encryption!"
                    )
                security_parameters = USMSecurityParameters.decode(
                    message.security_parameters
                )
                decrypted = priv_method.decrypt_data(
                    key,
                    message.scoped_pdu.value,
                    security_parameters.authoritative_engine_id,
                    security_parameters.priv_params,
                )
                message = replace(
                    message, scoped_pdu=ScopedPDU.decode(decrypted)
                )
            except Exception as exc:
                # TODO Use a proper app-exception here
                raise SnmpError("DecryptionError") from exc

        return message

    async def send_discovery_message(
        self,
        transport_handler: Callable[[bytes], Awaitable[bytes]],
    ) -> DiscoData:
        # Via https://tools.ietf.org/html/rfc3414#section-4
        #
        # The User-based Security Model requires that a discovery process
        # obtains sufficient information about other SNMP engines in order to
        # communicate with them. Discovery requires an non-authoritative SNMP
        # engine to learn the authoritative SNMP engine's snmpEngineID value
        # before communication may proceed. This may be accomplished by
        # generating a Request message with a securityLevel of noAuthNoPriv, a
        # msgUserName of zero-length, a msgAuthoritativeEngineID value of zero
        # length, and the varBindList left empty. The response to this message
        # will be a Report message containing the snmpEngineID of the
        # authoritative SNMP engine as the value of the
        # msgAuthoritativeEngineID field within the msgSecurityParameters
        # field. It contains a Report PDU with the usmStatsUnknownEngineIDs
        # counter in the varBindList.

        request_id = get_request_id()
        security_params = USMSecurityParameters(
            authoritative_engine_id=b"",
            authoritative_engine_boots=0,
            authoritative_engine_time=0,
            user_name=b"",
            auth_params=b"",
            priv_params=b"",
        )
        discovery_message = Message(
            Integer(3),
            HeaderData(
                request_id,
                MESSAGE_MAX_SIZE,
                V3Flags(False, False, True),
                3,
            ),
            bytes(security_params),
            ScopedPDU(
                OctetString(),
                OctetString(),
                GetRequest(request_id, []),
            ),
        )
        payload = bytes(discovery_message)
        raw_response = await transport_handler(payload)
        response, _ = pop_tlv(raw_response, Sequence)

        response_msg = Message.from_sequence(response)

        response_id = response_msg.scoped_pdu.data.request_id
        if response_id != request_id:
            raise SnmpError(
                f"Invalid response ID {response_id} for request id {request_id}"
            )

        # The engine-id is available in two places: The response directly, and also
        # the Report PDU. In initial tests these values were identical, and
        # fetching them from the wrapping message would be easier. But because the
        # RFC explicitly states that it's the value from inside the PDU I picked it
        # out from there instead.
        auth_security_params = USMSecurityParameters.decode(
            response_msg.security_parameters
        )
        unknown_engine_ids = response_msg.scoped_pdu.data.varbinds[
            0
        ].value.pythonize()

        out = DiscoData(
            authoritative_engine_id=auth_security_params.authoritative_engine_id,
            authoritative_engine_boots=auth_security_params.authoritative_engine_boots,
            authoritative_engine_time=auth_security_params.authoritative_engine_time,
            unknown_engine_ids=unknown_engine_ids,
        )
        return out


def create() -> UserSecurityModel:
    return UserSecurityModel()


def default_security_params() -> bytes:
    return
