# Triage labels

Canonical five-role state machine used by the `triage` skill. Each role
maps to one GitHub label string.

| Role | Label string | Meaning |
|---|---|---|
| Needs evaluation | `needs-triage` | Maintainer must review before further action. |
| Waiting on reporter | `needs-info` | Awaiting clarification or repro steps from reporter. |
| AFK-ready | `ready-for-agent` | Fully specified; an agent can pick it up with zero human context. |
| Human-only | `ready-for-human` | Needs a human implementer (architectural judgment, design call). |
| Won't fix | `wontfix` | Out of scope or rejected. |

## Bootstrap

Create these labels once before the first triage pass:

```bash
gh label create needs-triage --color "FBCA04" --description "Maintainer needs to evaluate"
gh label create needs-info --color "D4C5F9" --description "Waiting on reporter"
gh label create ready-for-agent --color "0E8A16" --description "Fully specified, AFK-ready"
gh label create ready-for-human --color "1D76DB" --description "Needs human implementation"
gh label create wontfix --color "FFFFFF" --description "Will not be actioned"
```

## State transitions

- New issue → `needs-triage`
- After review, one of: `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`
- `needs-info` → `ready-for-agent` or `ready-for-human` once reporter responds
- Closing an issue removes the workflow labels.
