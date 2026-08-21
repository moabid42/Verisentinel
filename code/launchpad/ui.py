from __future__ import annotations

import html
import json

from launchpad.models import CandidateSet


def render_dashboard(candidate_set: CandidateSet) -> str:
    cards = "".join(_render_card(card, candidate_set) for card in candidate_set.candidates)
    escaped_engagement = html.escape(candidate_set.engagement_id)
    escaped_state = html.escape(candidate_set.state_version)
    escaped_matrix = html.escape(candidate_set.matrix_version)
    state_panel = _render_state_panel(candidate_set)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>IAM operator launchpad</title>
  <style>
    body {{ font: 16px/1.5 system-ui; margin: 0 auto; max-width: 1100px; padding: 2rem; }}
    .meta {{ color: #4b5563; overflow-wrap: anywhere; }}
    .state {{ background: #f3f4f6; border-radius: .75rem; margin: 1rem 0; padding: 1rem; }}
    .counts {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
    .counts strong {{ display: block; font-size: 1.35rem; }}
    .grid {{ display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit,minmax(280px,1fr)); }}
    article {{ border: 1px solid #d1d5db; border-radius: .75rem; padding: 1rem; }}
    button {{ margin-right: .5rem; padding: .55rem .8rem; }}
    .approve {{ background: #166534; color: white; border: 0; }}
    pre {{ white-space: pre-wrap; overflow-wrap: anywhere; }}
  </style>
</head>
<body>
  <h1>Attacker launchpad</h1>
  <p>{html.escape(candidate_set.objective)}</p>
  <p class="meta">Engagement {escaped_engagement}<br>
    State {escaped_state}<br>Matrix {escaped_matrix}</p>
  {state_panel}
  <h2>Green Agent candidates</h2>
  <label>Operator <input id="operator" required autocomplete="username"></label>
  <div class="grid">{cards or '<p>No admissible candidates.</p>'}</div>
  <p>
    <button onclick="decide('request_alternatives', null)">Request alternatives</button>
    <button onclick="decide('reject_all', null)">Reject all</button>
    <button onclick="decide('terminate', null)">Terminate</button>
  </p>
  <pre id="result" role="status"></pre>
  <script>
    async function decide(kind, candidateId) {{
      const operator = document.getElementById('operator').value.trim();
      if (!operator) {{
        document.getElementById('result').textContent = 'Operator is required.';
        return;
      }}
      const payload = {{
        engagement_id: {json.dumps(candidate_set.engagement_id)}, decision: kind,
        candidate_id: candidateId, state_version: {json.dumps(candidate_set.state_version)},
        matrix_version: {json.dumps(candidate_set.matrix_version)}, operator, reason: ''
      }};
      const response = await fetch('/engagements/{escaped_engagement}/decision', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }});
      document.getElementById('result').textContent = await response.text();
    }}
  </script>
</body>
</html>"""


def _render_state_panel(candidate_set: CandidateSet) -> str:
    analysis = candidate_set.state_analysis
    identity = html.escape(candidate_set.identity or "Not supplied")
    scope = html.escape(candidate_set.scope or "Not supplied")
    if analysis is None:
        return f"""<section class="state">
  <h2>Current IAM state</h2>
  <p><strong>Identity:</strong> {identity}<br><strong>Scope:</strong> {scope}</p>
  <p>Coverage counts are unavailable.</p>
</section>"""
    monitored = len(analysis.monitored_permission_indices)
    unmonitored = len(analysis.unmonitored_permission_indices)
    available = monitored + unmonitored
    return f"""<section class="state">
  <h2>Current IAM state</h2>
  <p><strong>Identity:</strong> {identity}<br><strong>Scope:</strong> {scope}</p>
  <div class="counts">
    <span><strong>{available}</strong> available permissions</span>
    <span><strong>{monitored}</strong> monitored permissions</span>
    <span><strong>{unmonitored}</strong> unmonitored permissions</span>
  </div>
</section>"""


def _render_card(card, candidate_set: CandidateSet) -> str:
    proposal = card.proposal
    validation = card.validation
    required = ", ".join(card.required_permissions) or "None"
    covered = ", ".join(validation.covered_permissions) or "None"
    uncovered = ", ".join(validation.uncovered_permissions) or "None"
    detections = ", ".join(validation.matching_detection_ids) or "None"
    capabilities = ", ".join(card.expected_capabilities) or "None declared"
    title = card.technique_title or proposal.technique_id
    return f"""<article>
  <h3>#{proposal.rank} {html.escape(title)}</h3>
  <p class="meta">Technique {html.escape(proposal.technique_id)}<br>
    Action {html.escape(proposal.action_id)}</p>
  <p><strong>Identity:</strong> {html.escape(proposal.identity)}<br>
     <strong>Target:</strong> {html.escape(proposal.target)}</p>
  <p><strong>Required and available:</strong> {html.escape(required)}</p>
  <p><strong>Covered footprint:</strong> {html.escape(covered)}<br>
     <strong>Uncovered footprint:</strong> {html.escape(uncovered)}<br>
     <strong>Matching detections:</strong> {html.escape(detections)}</p>
  <p><strong>Expected capabilities:</strong> {html.escape(capabilities)}</p>
  <p><strong>Validator:</strong> {html.escape(validation.explanation)}</p>
  <p><strong>Proposer rationale:</strong> {html.escape(proposal.rationale)}</p>
  <button class="approve"
    onclick="decide('approve', {json.dumps(proposal.candidate_id)})">Approve</button>
  <button onclick="decide('reject', {json.dumps(proposal.candidate_id)})">Reject</button>
</article>"""
