# Jev decisions

Jev can answer two questions about a sampled conversation, and each one has to be switched on separately.

**Which skill to load.** Jev receives a short message excerpt and a menu of eligible skill names and descriptions. Its valid answer changes the selected skill; a timeout or failed request keeps the normal trigger-matching result.

**Which model answers a chat message.** Jev judges how hard the message is -- simple, medium or complex -- and the chat runs on the model you mapped that level to. This happens only while the session's model is set to **Auto (Jev)** in the chat model picker. A timeout, a failed request, or a model your account cannot run leaves the chat on the model it was already using. See [Letting Jev pick the model](#letting-jev-pick-the-model) below.

Both are off by default. Turning on the Decisions switch does not start either one: skill selection also needs `skills.max_triggered` above zero, and model routing also needs you to pick **Auto (Jev)** for a session.

## What changes

Only these two things use Jev. Skill deduplication and scheduled notifications do not change. Mandatory skills, custom-agent exclusions, project access rules and the automatic skill limit still apply. Scheduled jobs, sub-agents, sessions running on a connected crew, and anything an app sends are never routed -- each already has its own model setting, and nobody is watching what those cost at the moment they run.

| State | Skill selection |
|---|---|
| Disabled | Normal trigger matching |
| Enabled, outside the sample | Normal trigger matching |
| Enabled, sampled, valid answer | Jev's selection |
| Timeout, refusal or invalid answer | Normal trigger matching |

A valid answer can choose one skill or explicitly choose none. Choosing none is not a failed request. There is no shadow mode that asks Jev only to discard its answer.

## Configure before enabling

Use Settings > Developer > Feature Previews for the Decisions switch. It records your consent in `decisions_consent.json` in the gateway's data directory, so it applies across devices. That file is deliberately separate from `config.json`: an agent can edit `config.json`, and an agent must not be able to switch on the sending of your own messages. Only the dashboard owner can flip the switch. The card shows the address messages would be sent to, and your consent is recorded for that address: if `provider.endpoint` is changed later, nothing is sent until you turn the switch off and on again. An older backend without this switch keeps it disabled.

The remaining settings live in `config.json`:

The configuration shape is:

```json
{
  "decisions": {
    "bucket": 10,
    "history_budget_chars": 0,
    "model_route": {
      "simple": "claude-haiku-4.5",
      "medium": "claude-opus-4.8",
      "complex": "claude-fable-5.1"
    },

    "provider": {
      "endpoint": "https://api.typesafe.ai/v1/systemone",
      "api_key": "secret://TYPESAFE_API_KEY",
      "model": "jev-latest",
      "timeout_ms": 1000
    }
  }
}
```

Create the API-key secret through the existing [secrets vault](secrets-vault.md) under the name `TYPESAFE_API_KEY`; that is the only vault entry this feature reads, and only for the default Jev endpoint. The reference above is a placeholder, not a working key.

`bucket` chooses a percentage of sessions. It is a fixed sample, not a random draw per message. A session stays selected or unselected while its key and bucket remain unchanged. `0` samples none and `100` samples all otherwise eligible sessions. A value that is not a whole number reads as `0`, so a typo never widens the sample.

`history_budget_chars` bounds how much of the conversation so far is sent with one decision, in characters, on top of the current message. **Its default is `0`: no earlier turns are sent.** Raise it and earlier user and assistant turns are added newest first until the budget is spent, with the last one admitted clipped to fit. At most the 20 most recent turns are read, so a budget far above a few thousand characters stops adding turns. Tool output is never sent. A value that does not parse reads as `0`, so a typo never widens what leaves the machine.

This setting alone does not permit the transfer. Your consent record holds a **ceiling** for it, and Kiro Crew sends the smaller of the two. Lowering the setting works on its own; raising it above the ceiling does nothing until you consent again with the larger figure. The reason is that `config.json` can be written by an agent working on your machine, while the consent record cannot: if the permission lived only in the settings file, an agent reading your conversation could raise it and send that conversation. A consent recorded before this ceiling existed has no figure in it, which reads as `0` — so an upgrade never starts sending your earlier turns.

`model_route` says which model answers a message at each difficulty level. **Every level starts empty, which means "leave it alone".** No model is named for you on purpose: accounts differ in which models they are offered, and a name you cannot use would fail on the first message rather than when you set it. The block above is the example to copy from — put in the ids your own model picker shows.

An empty level does not turn the feature off. Jev is still asked, the answer is still recorded, and the reply still shows it — it reads `complex → (unpinned)`. The message just runs on the model the chat was already using. That is on purpose: you can watch which level your messages land in for a while, and then pin only the levels worth moving.

The three keys above are the only ones read; anything else is ignored. `auto` means the same as empty. If you name a model your account cannot run, that message also stays put, and the log below says which of the two happened, so nothing is dropped silently.

This setting alone changes nothing: a chat is only routed while its model is set to **Auto (Jev)**.

`skills.max_triggered` must be greater than zero to allow automatic selection. Its default is zero, which disables automatic selection even when the Decisions switch is on. Jev selects at most one skill and does not raise that limit.

After setting the provider and sampling values, enable the switch only if the data transfer below is acceptable. Turn it off to return to normal trigger matching. Old `preview` and per-point mode values do not enable this new behavior.

## Letting Jev pick the model

Open the model picker under the chat box and choose **Auto (Jev)**. The entry appears only when the Decisions switch is on and your organisation allows the feature, so if you do not see it, turn the switch on first.

That alone gets you the reading: each message is judged and the reply says which level it landed in, while every level is still unpinned so nothing moves. Fill in `model_route` above when you want messages actually routed — each level you pin starts taking effect on the next message.

From then on, each message you type is judged once, and a message whose level you have pinned is answered by that model. A message that needs a plan or a trade-off can go to a stronger model; a rename or a lookup can go to a cheaper one. A level you have not pinned is reported and left alone.

**This can cost more.** Routing a message to a stronger model spends more than staying on your usual one. You chose that when you picked **Auto (Jev)**, and you can undo it in one click: pick any model in the same picker and the routing stops immediately. Picking a model by hand is never overridden -- it is your answer to the same question Jev was being asked.

A few things worth knowing. The choice belongs to one chat, not to the whole app, and it lasts until you change it or the gateway restarts — a restart leaves the chat on its usual model, and you pick **Auto (Jev)** again. It is not written to disk on purpose: the file it would live in can be edited by an agent working on your machine, and this choice can cost you money, so nothing but your own click in the picker turns it on. A message sent by a scheduled job, a sub-agent or an app is never routed. The chat does not switch back after each message: if Jev is unavailable for the next one, that message runs on whatever the last one used. And the reply carries a small line saying which level Jev picked, which model answered, and which model would have answered otherwise -- with a thumbs pair, so you can say it got it wrong.

## Data and waiting time

Enabling Jev allows the message excerpt to leave the machine -- with the candidate skill descriptions for a skill choice, and on its own for a model choice. It does not send your earlier turns unless you both raise `history_budget_chars` and consent to a ceiling for it, after which that many characters of earlier user and assistant turns from the same conversation leave the machine as well. Credential and suspicious-URL checks refuse matching requests, but they are not a guarantee that all private content is detected. Do not enable the feature for content that must stay local.

A sampled selection waits for a bounded answer. `timeout_ms` controls the provider budget; its default is 1000 milliseconds, and the wait is capped at ten seconds whatever that value says. A missing key, unavailable provider or short budget can make the feature fall back without changing the selected skills. There is no automatic retry.

## What a decision leaves on its reply

When a sampled turn asks Jev something, the reply that turn produces carries a record of each decision. A record belongs to one reply. It is never copied onto a later one, and a turn that made no decision carries nothing at all. A turn that made both decisions carries both, one line each.

The records travel with the message, not in a side channel, so they are there when you scroll back to that reply and there when a second window opens the same chat. A skill record holds what trigger matching chose, what Jev chose, whether the two agreed, the probability Jev reported and a few counts about the menu it was given. A model record holds the difficulty level, the model that answered, the model that would have answered otherwise, the probability and how long the decision took. Neither holds your message or the skill descriptions.

Each line carries a thumbs pair, and you can record whether a choice was right. The verdict is `right` or `wrong`, and it names which of the two answers you are judging -- Jev's or the normal trigger-matching one. Sending it again with a different verdict records the change of mind; sending it with the verdict spelled out as `null` takes your earlier one back. Leaving the field out altogether is refused instead, so a request that lost it does not read as taking a verdict back. Each of these appends one row to the day-file described below and never edits a row already there, so the log reads as a history rather than a current opinion. If the day-file is full the verdict is refused rather than quietly dropped, so a recorded verdict means a written one.

It is an owner-only request (`POST /api/decisions/feedback`), refused for anyone but the dashboard owner, for the same reason the Decisions switch is. The verdicts land in the same daily JSONL files as the decisions, so counting them is a `jq` job over `~/.kiro/crew/decisions/*.jsonl`.

## Basic logs

Operational records are JSONL day-files under the gateway's data home, in the `decisions` directory. That directory is read-only to agents working on your machine -- by name, so a link planted at that name does not stand in for it -- so a verdict in it is one you gave. They contain the point name, hashed session identifier, elapsed time, bounded answer data and error categories. They do not contain the message body, conversation history, candidate descriptions or credentials.

A model choice writes one row for the question asked and — when the chosen model could actually be used — one further row carrying the level (`tier`), the model used (`model_chosen`), the model that would have been used (`baseline_model`) and the probability. When the level came back but could not be applied, the second row carries an error word instead: `model-not-advertised` if your account cannot run that model, `no-switch-seam` if the chat backend cannot change model mid-conversation, and `switch-failed` if it refused. Those are written on purpose, so "Jev answered and nothing happened" is visible rather than silent.

A skill selection writes one row for the question asked, carrying a `turn_id` and the number of candidates, and — when a usable answer came back — one further row for the outcome, carrying both selections: `baseline` is what trigger matching would have injected, `jev` is what was injected, `agree` says whether the two sets match, `p` is the answer's probability, and `tokens_saved` estimates the skill-body characters the difference saves, divided by four. That estimate is a rough one, and a negative value means the selection cost more than trigger matching would have. `history_chars` and `truncated` say how much conversation the request carried. A refused, timed-out or unusable turn writes only the question row, with its error category, because an agreement figure needs an answer to compare against. So one selection is two rows, and a row count is not a count of decisions.

These are diagnostic records, not a billing report. This feature does not provide a decisions report command. Each day-file stops growing at 8 MiB (further rows that day are dropped, with one warning), and day-files older than 14 days are deleted by the next write, so the log stays a bounded number of bounded files. A missing row alone is not proof that an answer was applied.

The provider mapping is tested locally against a loopback server. A real Jev call requires your API key; local tests do not establish real service latency or account compatibility.
