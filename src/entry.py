import time
from dataclasses import dataclass
from typing import Any, Final
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


@dataclass(frozen=True, slots=True)
class ModHandler:
    bot_handler: AbstractBotHandler
    client: Client

    def handle_message(self, message: dict[str, Any]) -> str:
        sender_user = self._get_sender_user(message, self.client)
        if not self._check_authorized(message, sender_user):
            return "You are not authorized to use ModBot."

        content = message["content"].removeprefix("@**ModBot**").strip()
        if content == "help":
            return self._handle_help(message)

        if content.startswith("timeout"):
            return self._validate_timeout(message, content, sender_user)

        self.bot_handler.react(message, "interrobang")
        return "Not a valid command. Send \"help\" for usage information."

    def _get_sender_user(self, message: dict[str, Any], client: Client) -> dict[str, Any]:
        sender_email = message['sender_email']
        sender_user = client.call_endpoint(
            url=f"/users/{sender_email}",
            method="GET",
        )
        return sender_user

    def _handle_help(self, message: dict[str, Any]) -> str:
        self.bot_handler.react(message, "thinking")
        return (
            "Use this bot with any of the following commands:"
            "\n* `@ModBot timeout <userid> <minutes>` : Timeout a user by user id for a specified number of minutes"
            "\n* `@ModBot help` : Display help message"
        )

    def _check_authorized(self, message: dict[str, Any], sender_user: dict[str, Any]) -> bool:
        if sender_user["user"]["role"] not in MOD_ROLES:
            return False
        else:
            return True

    def _validate_timeout(
        self,
        message: dict[str, Any],
        content: str,
        sender_user: dict[str, Any],
    ) -> str:
        content_tokens = content.split()

        if len(content_tokens) != TIMEOUT_TOKEN_COUNT:
            return "Usage: `@ModBot timeout <user id> <minutes>`"

        if not content_tokens[1].isdigit():
            return "Error: User ID must be a number."

        if not content_tokens[2].isdigit():
            return "Error: Minutes must be a number."

        user_id_to_timeout = int(content_tokens[1])
        user_to_timeout = self.client.get_user_by_id(user_id_to_timeout)
        if user_to_timeout["result"] != "success":
            return user_to_timeout["msg"]

        user_full_name = user_to_timeout["user"]["full_name"]
        if user_to_timeout["user"]["role"] in MOD_ROLES or user_to_timeout["user"]["user_id"] == MODBOT_USER:
            return f"User @**{user_full_name}|{user_id_to_timeout}** is immune to timeouts."

        return self._timeout_user(message, sender_user, user_full_name, content_tokens, user_id_to_timeout)

    def _timeout_user(
        self,
        message: dict[str, Any],
        sender_user: dict[str, Any],
        user_full_name: str,
        content_tokens: list[str],
        user_id_to_timeout: int,
    ) -> str:
        timeout_seconds = int(content_tokens[2]) * 60 # given in minutes
        timeout_request_params = {
            "delete": [user_id_to_timeout]
        }
        timeout_response = self.client.update_user_group_members(MEMBER_GROUP, timeout_request_params)
        if timeout_response["result"] != "success":
            return timeout_response["msg"]

        current_time_s = int(time.time())
        untimeout_time_s = current_time_s + timeout_seconds
        self.bot_handler.storage.put(str(user_id_to_timeout), untimeout_time_s)

        sender_full_name = sender_user["user"]["full_name"]
        sender_user_id = sender_user["user"]["user_id"]
        response = f"User @**{user_full_name}|{user_id_to_timeout}** has been timed out by @**{sender_full_name}|{sender_user_id}** until {time.ctime(untimeout_time_s)} UTC."
        self.bot_handler.send_message(dict(
            type='stream',
            to="modlog",
            subject="Timeouts",
            content=response,
        ))
        return response


class Default(WorkerEntrypoint):
    def _get_client(self):
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

        try:
            payload: dict[str, Any] | None = await request.json()
            if not payload:
                return Response.json({"error": "Missing request content"}, status=400)
            message: dict[str, Any] = payload.get("message")
            if not message:
                return Response.json({"error": "Missing 'message' in request"}, status=400)
            handler = ModHandler(bot_handler, client)
            reply_str = handler.handle_message(message)
            bot_handler.send_reply(message, reply_str)
            return Response(json.dumps({"result": "success"}), status=200)

        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500)


    async def scheduled(self, controller, env, ctx):
        client = self._get_client()
        timeout_data = client.get_storage()["storage"]
        user_groups = client.get_user_groups()["user_groups"]
        member_group = next((group for group in user_groups if group["id"] == MEMBER_GROUP), None)
        if not member_group:
            raise Exception("Member group not found.")

        member_group_members = member_group["members"]
        current_time_ms = int(time.time())
        for user_id in timeout_data:
            if int(user_id) not in member_group_members and int(timeout_data[user_id]) < current_time_ms:
                untimeout_request_params = {
                    "add": [int(user_id)]
                }
                untimeout_response = client.update_user_group_members(MEMBER_GROUP, untimeout_request_params)
                if untimeout_response["result"] != "success":
                    raise Exception(untimeout_response["msg"])

                untimedout_user = client.get_user_by_id(user_id)
                user_full_name = untimedout_user["user"]["full_name"]
                log_message = f"User @**{user_full_name}|{user_id}** timeout has been lifted."
                client.send_message(dict(
                    type='stream',
                    to="modlog",
                    subject="Timeouts",
                    content=log_message,
                ))
