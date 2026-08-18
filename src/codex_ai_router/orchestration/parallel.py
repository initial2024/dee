from concurrent.futures import ThreadPoolExecutor
from .judge import judge
from ..agents.reviewer import review


def parallel_review(local, api, diff: str, risk: str, policy=None):
    if policy and (not policy.providers.permits("local") or not policy.providers.permits("api")):
        from ..result import AgentResult
        return AgentResult("ESCALATE", "Parallel review blocked by provider policy", risk=risk, needs_escalation=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(review, local, diff, risk)
        two = pool.submit(review, api, diff, risk)
        return judge(one.result(), two.result())
