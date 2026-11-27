"""Typed aiohttp application keys (aiohttp's recommended replacement for string keys)."""
from aiohttp import web

SESSIONS = web.AppKey("sessions", object)
CONNECTORS = web.AppKey("connectors", object)
WORKFLOWS = web.AppKey("workflows", object)
MOCKS = web.AppKey("mocks", object)
ACTIONS = web.AppKey("actions", object)
STATE = web.AppKey("state", object)
AGENT = web.AppKey("agent", object)
LLM = web.AppKey("llm", object)
INTERNAL_TOKEN = web.AppKey("internal_token", object)
CHAT_LIMIT = web.AppKey("chat_limit", object)
VOICE_LIMIT = web.AppKey("voice_limit", object)
LOGIN_GUARD = web.AppKey("login_guard", object)
LOCKS = web.AppKey("locks", object)
REDIS_CLIENT = web.AppKey("redis_client", object)
SIGNUP = web.AppKey("signup", object)
DURABLE = web.AppKey("durable", object)
