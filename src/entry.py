import time
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

class ModHandler:
    def handle_message(self, message: dict[str, Any], bot_handler: AbstractBotHandler, client: Client) -> None:
        mod_roles = {OWNER_ROLE, ADMIN_ROLE, MOD_ROLE}
        sender_email = message['sender_email']
        sender_user = client.call_endpoint(
            url=f"/users/{sender_email}",
            method="GET",
        )

        if sender_user["user"]["role"] not in mod_roles:
            bot_handler.send_reply(message, "You are not authorized to use ModBot.")
            return


        help_str = (
            "Use this bot with any of the following commands:"
            "\n* `@ModBot timeout <userid> <minutes>` : Timeout a user by user id for a specified number of minutes"
            "\n* `@ModBot help` : Display help message"
        )

        content = message["content"].removeprefix("@**ModBot**").strip()
        if content == "help":
            bot_handler.send_reply(message, help_str)
            bot_handler.react(message, "thinking")
            return

        if content.startswith("timeout"):
            content_tokens = content.split()

            if len(content_tokens) == TIMEOUT_TOKEN_COUNT:
                try:
                    user_id_to_timeout = int(content_tokens[1])
                except ValueError:
                    bot_handler.send_reply(message, "Error: User ID must be a number.")
                    return

                try:
                    timeout_seconds = int(content_tokens[2]) * 60 # given in minutes
                except ValueError:
                    bot_handler.send_reply(message, "Error: Minutes must be a number.")
                    return

                user_to_timeout = client.get_user_by_id(user_id_to_timeout)
                if user_to_timeout["result"] != "success":
                    bot_handler.send_reply(message, user_to_timeout["msg"])
                    return

                timeout_request_params = {
                    "delete": [user_id_to_timeout]
                }
                timeout_response = client.update_user_group_members(MEMBER_GROUP, timeout_request_params)
                if timeout_response["result"] != "success":
                    bot_handler.send_reply(message, timeout_response["msg"])
                    return

                current_time_s = int(time.time())
                untimeout_time_s = current_time_s + timeout_seconds
                bot_handler.storage.put(str(user_id_to_timeout), untimeout_time_s)
                user_full_name = user_to_timeout["user"]["full_name"]
                sender_full_name = sender_user["user"]["full_name"]
                sender_user_id = sender_user["user"]["user_id"]
                response = f"User @**{user_full_name}|{user_id_to_timeout}** has been timed out by @**{sender_full_name}|{sender_user_id}** until {time.ctime(untimeout_time_s)} UTC."
                bot_handler.send_message(dict(
                    type='stream',
                    to="modlog",
                    subject="Timeouts",
                    content=response,
                ))
                bot_handler.send_reply(message, response)
            else:
                bot_handler.send_reply(message, "Usage: `@ModBot timeout <user id> <minutes>`")
            return
        else:
            content = "Not a valid command. Send \"help\" for usage information."
            bot_handler.send_reply(message, content)
            bot_handler.react(message, "interrobang")


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
            handler = ModHandler()
            handler.handle_message(message, bot_handler, client)
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
            if user_id not in member_group_members and int(timeout_data[user_id]) < current_time_ms:
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
