"""The words of the cards system, spelled once (services/cards): a button's
kind, an outcome's state, and the sentences a person reads. The dashboard
mirrors the kinds and states in ``lib/kinds/card.ts``."""

BUTTON_DO = "do"
BUTTON_SAY = "say"
BUTTON_KINDS = (BUTTON_DO, BUTTON_SAY)

# The kinds of list a list card shows (the pages' own lists).
LIST_TASKS = "tasks"
LIST_TRIGGERS = "triggers"
LIST_NOTIFICATIONS = "notifications"
LIST_MACHINES = "machines"
LIST_MCPS = "mcps"
LIST_KINDS = (LIST_TASKS, LIST_TRIGGERS, LIST_NOTIFICATIONS, LIST_MACHINES, LIST_MCPS)

OUTCOME_DONE = "done"
OUTCOME_RUNNING = "running"
OUTCOME_FAILED = "failed"
OUTCOME_STATES = (OUTCOME_DONE, OUTCOME_RUNNING, OUTCOME_FAILED)

# A card lives as long as a parked prompt would (the prompt wait of
# core/session/session_state, three days).
CARD_TTL_S = 3 * 24 * 3600

# A session with nobody at the keyboard (a scheduled run, a trigger, a
# call, a meeting) never gets a card: nobody would press it.
NO_PERSON = "A card shows only in a chat with a person in it."
CARD_ELSEWHERE = "This card is in a chat you cannot open."
PRESSED_ALREADY = "This button was pressed already."
ANSWERED_ALREADY = "This card was answered already."
EXPIRED = "This card has expired. Ask again."
CHAT_GONE = "The chat of this card is gone."
NO_SUCH_BUTTON = "This card has no such button."
NO_SUCH_CARD = "No such card."
NO_SUCH_LIST = "There is no list of that kind."
NO_ANSWER = "The platform did not answer. The server log has the details."
INTERRUPTED = "The action was interrupted before it finished. Ask the agent for a new card."
CHAT_NOT_OPEN = "You can no longer open this chat."
SHARED_NEEDS_EDITOR = "Only editors and managers answer in the agent's shared chats."
# A card about the asker's own files, memory or personal apps, pressed by
# someone else: it never acts on the presser's same-named things.
ASKERS_OWN = "This card is about {name}'s own {what}. Ask the agent for a card of yours."
# A say press where the presser's session may not run (another person's
# computer, or a platform machine whose role rule keeps the presser off it):
# the composer moves the chat first, a card cannot.
PLACE_NOT_YOURS = "This chat runs on a computer you cannot use. Write your answer in the chat instead."
# The composer's own sentence for a full queue (ws/dashboard_dispatch).
TOO_MANY_QUEUED = "Too many queued messages. Wait for the current turn to finish."
# A queued card answer is the person's press: nobody cancels it.
CARD_ANSWER_STAYS = "A card's answer is not taken back."
TITLE_TOO_LONG = "The title is too long."
ONE_DO_BUTTON = "One card does one thing. Draw a second card for the other."
TEXT_TOO_LONG = "The text is too long."
# The caption under a person's message a card sent (mirrored).
FROM_CARD = "From a card"
