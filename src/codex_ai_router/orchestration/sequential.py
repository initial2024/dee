from ..agents.coder import agent_loop


def api_local(api, local, task, root, risk, max_iterations):
    proposal = agent_loop(api, task, root, risk, max_iterations)
    if proposal.needs_escalation:
        return proposal
    return agent_loop(local, "Review API proposal: " + proposal.summary, root, risk, max_iterations)
