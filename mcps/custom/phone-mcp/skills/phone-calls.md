## Phone Calls

Use `make_call` to start calls, `wait_for_call` to monitor, `answer_call_question` to respond to mid-call questions, and `get_call_status` to check progress.

### Key Rules

- **Calls are non-blocking** — `make_call` returns immediately with a `call_id`. You must then `wait_for_call` to monitor.
- **NEVER call without explicit user approval.** Always confirm:
  - The business/person name and phone number
  - The purpose of the call (reservation, inquiry, etc.)
  - Any specific instructions for the call
- Provide the caller agent with clear, specific instructions:
  - What to say (e.g., "Book a table for 4 on Saturday at 9 PM")
  - The business name and context
  - Language to use
  - **Tell the caller to use `[QUESTION:]` if they need information they don't have**
- After the call completes, report the outcome to the user.

### Call Monitoring Loop

After starting a call, follow this loop:

```
1. make_call(phone_number, task_description, instructions) → get call_id
2. wait_for_call(call_id) → blocks until event
3. If event = "question":
   - Show the user the FULL call transcript so far
   - Show the caller's question
   - Decide based on context or ask the user
   - answer_call_question(call_id, answer)
   - Go to step 2 (wait again)
4. If event = "completed":
   - Report outcome with full transcript to user
5. If event = "failed":
   - Inform user, suggest retry if appropriate
```

**IMPORTANT:** When reporting call events, always include the **call transcript** returned by `wait_for_call` or `get_call_status`. The user wants to see what was said. Show it as a conversation log.

### Answering Caller Questions

When the caller agent asks a question mid-call:
- **Scheduling conflicts** → check the calendar if available, approve or decline accordingly.
- **Preference questions** (seating, dietary, special requests) → ask the user if not obvious from context.
- **Simple factual questions** (name, number of people) → answer directly from the call context.
- **Anything uncertain** → ask the user. You can interact with the user while the call is on hold.
