import time
from typing import Any, Dict, Optional, Final
from workers import WorkerEntrypoint, Request, Response
import json

from zulip import Client
from zulip_bots.lib import AbstractBotHandler, ExternalBotHandler, use_storage

MEMBER_GROUP: Final[int] = 1522351

class ModHandler:
    def handle_message(self, message: Dict[str, Any], bot_handler: AbstractBotHandler, client: Client) -> None:
        mod_roles = {100, 200, 300}
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

        content = message["content"].strip()
        if content == "help":
            bot_handler.send_reply(message, help_str)
            return

        if content.startswith("timeout"):
            content_tokens = content.split()

            if len(content_tokens) == 3:
                user_id_to_timeout = content_tokens[1]
                try:
                    timeout_minutes = int(content_tokens[2])
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
                    untimeout_time_s = current_time_s + (timeout_minutes * 60)
                    with use_storage(bot_handler.storage, [user_id_to_timeout]) as storage:
                        storage.put(user_id_to_timeout, untimeout_time_s)

                    response = f"User {user_id_to_timeout} has been timed out until {time.ctime(untimeout_time_s)} UTC."
                    bot_handler.send_reply(message, response)
                except ValueError:
                    bot_handler.send_reply(message, "Error: Minutes must be a number.")
            else:
                bot_handler.send_reply(message, "Usage: `@ModBot timeout <user id> <minutes>`")
            return

        content = "beep boop"
        bot_handler.send_reply(message, content)
        bot_handler.react(message, "wave")

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
            payload: Optional[Dict[str, Any]] = await request.json()
            if not payload:
                return Response.json({"error": "Missing request content"}, status=400)
            message: Dict[str, Any] = payload.get("message")
            if not message:
                return Response.json({"error": "Missing 'message' in request"}, status=400)
            handler = ModHandler()
            handler.handle_message(message, bot_handler, client)
            return Response(json.dumps({"result": "success"}), status=200)
            
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500)


    async def scheduled(self, controller, env, ctx):
        client = self._get_client()
        timeout_data = client.get_storage()
        current_time_ms = int(time.time())
        for user_id in timeout_data:
            if timeout_data[user_id] < current_time_ms:
                untimeout_request_params = {
                    "add": [user_id]
                }
                untimeout_response = client.update_user_group_members(MEMBER_GROUP, untimeout_request_params)
                if untimeout_response["result"] != "success":
                    raise Exception(untimeout_response["msg"])
