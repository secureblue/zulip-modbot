import time
from dataclasses import dataclass, field
from typing import Any, Final, Self
from workers import WorkerEntrypoint, Request, Response
import json

from zulip import Client
from zulip_bots.lib import AbstractBotHandler, ExternalBotHandler

TIMEOUT_TOKEN_COUNT: Final[int] = 3

MEMBER_GROUP: Final[int] = 1522351
OWNER_ROLE: Final[int] = 100
ADMIN_ROLE: Final[int] = 200
MOD_ROLE: Final[int] = 300
MODBOT_USER: Final[int] = 1028937
MOD_ROLES: Final[set[int]] = {OWNER_ROLE, ADMIN_ROLE, MOD_ROLE}


@dataclass(frozen=True, slots=True, kw_only=True)
class User:
    id: int
    role: int
    full_name: str

    mention: str = field(init=False)
    moderator: bool = field(init=False)

    @classmethod
    def get(cls, client: Client, user_id_str: str | int) -> Self:
        if isinstance(user_id_str, str) and not user_id_str.isdigit():
            raise ValueError("Error: User ID must be a number.")
        user_id = int(user_id_str)

        response: dict["str", Any] = client.get_user_by_id(user_id)
        if response["result"] != "success":
            raise ValueError(response["msg"])
        response = response["user"]

        return cls(
            id=user_id,
            role=int(response["role"]),
            full_name=response["full_name"],
        )

    def __post_init__(self) -> None:
        mention = f"@**{self.full_name}|{self.id}**"
        object.__setattr__(self, "mention", mention)

        is_moderator = self.role in MOD_ROLES or self.id == MODBOT_USER
        object.__setattr__(self, "moderator", is_moderator)


@dataclass(frozen=True, slots=True)
class ModHandler:
    bot_handler: AbstractBotHandler
    client: Client

    def handle_message(self, message: dict[str, Any]) -> str:
        sender_user = User.get(self.client, message["sender_id"])
        if not sender_user.moderator:
            return "You are not authorized to use ModBot."

        content = message["content"].removeprefix("@**ModBot**").strip()
        content_tokens = content.split()

        if content == "help":
            return self._handle_help(message)

        if content.startswith("timeout"):
            return self._validate_timeout(message, content_tokens, sender_user)

        self.bot_handler.react(message, "interrobang")
        return 'Not a valid command. Send "help" for usage information.'

    def _handle_help(self, message: dict[str, Any]) -> str:
        self.bot_handler.react(message, "thinking")
        return (
            "Use this bot with any of the following commands:"
            "\n* `@ModBot timeout <userid> <minutes>` : Timeout a user by user id for a specified number of minutes"
            "\n* `@ModBot help` : Display help message"
        )

    def _validate_timeout(
        self,
        message: dict[str, Any],
        tokens: list[str],
        sender_user: User,
    ) -> str:
        if len(tokens) != TIMEOUT_TOKEN_COUNT:
            return "Usage: `@ModBot timeout <user id> <minutes>`"

        if not tokens[2].isdigit():
            return "Error: Minutes must be a number."

        try:
            target_user = User.get(self.client, tokens[1])
        except ValueError as e:
            return str(e)

        if target_user.moderator:
            return f"User {target_user.mention} is immune to timeouts."

        timeout_seconds = int(tokens[2]) * 60  # given in minutes
        return self._timeout_user(message, timeout_seconds, sender_user, target_user)

    def _timeout_user(
        self,
        message: dict[str, Any],
        timeout_seconds: int,
        sender_user: User,
        target_user: User,
    ) -> str:
        timeout_response = self.client.update_user_group_members(
            MEMBER_GROUP, {"delete": [target_user.id]}
        )
        if timeout_response["result"] != "success":
            return str(timeout_response["msg"])

        current_time_s = int(time.time())
        untimeout_time_s = current_time_s + timeout_seconds
        self.bot_handler.storage.put(str(target_user.id), untimeout_time_s)

        reply_str = f"User {target_user.mention} has been timed out by {sender_user.mention} until {time.ctime(untimeout_time_s)} UTC."
        self.bot_handler.send_message(dict(
            type='stream',
            to="modlog",
            subject="Timeouts",
            content=reply_str,
        ))
        return reply_str


class Default(WorkerEntrypoint):
    def _get_client(self) -> Client:
        return Client(
            email=self.env.ZULIP_EMAIL,
            api_key=self.env.ZULIP_API_KEY,
            site=self.env.ZULIP_SITE
        )

    async def fetch(self, request: Request) -> Response:
        client = self._get_client()
        bot_handler = ExternalBotHandler(
            client=client,
            root_dir=None,
            bot_details={"name": "ModBot"}
        )

        def respond(status_code: int, message: str = "") -> Response:
            result = "success" if status_code == 200 else "error"
            json_str = json.dumps({ "result": result, "msg": message })
            return Response(json_str, status=status_code)

        try:
            payload: dict[str, Any] | None = await request.json()
            if not payload:
                return respond(400, "Missing request content")
            message: dict[str, Any] | None = payload.get("message")
            if not message:
                return respond(400, "Missing 'message' in request")

            mod_handler = ModHandler(bot_handler, client)
            reply_str = mod_handler.handle_message(message)
            bot_handler.send_reply(message, reply_str)
            return respond(200)
        except Exception as e:
            return respond(500, str(e))


    async def scheduled(self, controller: Any, env: Any, ctx: Any) -> None:
        client = self._get_client()
        timeout_data = client.get_storage()["storage"]
        user_groups = client.get_user_groups()["user_groups"]
        member_group = next((group for group in user_groups if group["id"] == MEMBER_GROUP), None)
        if not member_group:
            raise Exception("Member group not found.")

        member_group_members = member_group["members"]
        current_time_s = int(time.time())
        for user_id in timeout_data:
            not_timed_out = int(user_id) in member_group_members
            timeout_sill_active = int(timeout_data[user_id]) > current_time_s
            if not_timed_out or timeout_sill_active:
                return

            untimeout_request_params = {
                "add": [int(user_id)]
            }
            untimeout_response = client.update_user_group_members(MEMBER_GROUP, untimeout_request_params)
            if untimeout_response["result"] != "success":
                raise Exception(untimeout_response["msg"])

            untimedout_user = User.get(client, user_id)
            log_message = f"User {untimedout_user.mention} timeout has been lifted."
            client.send_message(dict(
                type='stream',
                to="modlog",
                subject="Timeouts",
                content=log_message,
            ))
