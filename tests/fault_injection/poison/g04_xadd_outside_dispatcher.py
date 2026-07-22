# Poison fixture for guard 4 — XADD outside dispatcher/ (must fail lint).
redis.xadd("cp:http:normal", {"task_id": "tsk_poison"})
