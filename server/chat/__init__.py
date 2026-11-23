"""Chat (PR #6): the Agent view's conversation layer.

server/chat/classify.py holds the pure decision logic (rules,
steering, the planner prompt + planner-output parsing) — no DB,
no network, unit-testable on its own. The HTTP endpoints and the
planner/rules orchestration live in server/api/chat_api.py.
"""
