import sqlite3
from datetime import date, datetime
from typing import Any, Literal, Optional, Union

from langchain_core.runnables.config import ensure_config
from langchain_core.tools import tool

from evals.email_assistant.db import db_file


@tool
async def search_emails(
    queries: Optional[list[str] | str] = None,
    sender: Optional[str] = None,
    recipient: Optional[str] = None,
    start_date: Optional[date | datetime] = None,
    end_date: Optional[date | datetime] = None,
    thread_id: Optional[int] = None,
) -> dict:
    """Search emails based on the given queries and optional filters.
    queries: The search queries to match against the subject or body of the email. The queries are joined with an OR condition.
    sender: A substring to match against the sender email address.
        If you don't know the email domain, just search by username.
    recipient: A substring to match against the recipient email address.
        If you don't know the email domain, just search by username.
    start_date: The start date for the search range (inclusive, but be lenient on bounds).
    end_date: The end date for the search range (exclusive)
    thread_id: The thread ID of the email thread."""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    conditions = []
    params = []

    if queries:
        if isinstance(queries, str):
            queries = [queries]
        query_conditions = []
        for query in queries:
            for exact in query.split('"'):
                for tok in exact.split():
                    tok = tok.replace("*", "%")
                    query_conditions.append(
                        "(subject LIKE ? OR body LIKE ? OR sender LIKE ? OR recipient LIKE ?)"
                    )
                    params.extend([f"%{tok}%", f"%{tok}%", f"%{tok}%", f"%{tok}%"])
        conditions.append(f"({' OR '.join(query_conditions)})")

    if sender:
        conditions.append("sender LIKE ?")
        params.append(f"%{sender}%")
    if recipient:
        conditions.append("recipient LIKE ?")
        params.append(f"%{recipient}%")
    if start_date:
        conditions.append("timestamp >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("timestamp < ?")
        params.append(end_date)
    if thread_id:
        conditions.append("thread_id = ?")
        params.append(thread_id)

    where_clause = " AND ".join(conditions)
    if where_clause:
        where_clause = " WHERE " + where_clause
    cursor.execute(f"SELECT * FROM emails{where_clause}", params)
    results = cursor.fetchall()
    conn.close()
    keys = [column[0] for column in cursor.description]
    return {
        "results": [dict(zip(keys, row)) for row in results],
        "count": len(results),
    }


@tool
async def search_calendar_events(
    queries: Optional[list[str] | str] = None,
    start_date: Optional[datetime | date] = None,
    end_date: Optional[datetime | date] = None,
) -> dict:
    """Search calendar events based on the given queries and optional date range.

    Args:
        queries (list[str] | str | null): The search queries to match against the title or description of the event. The queries are joined with an OR condition.
            Examples:
                - queries: ["standup", "walk the dog"] (search for events containing either of these terms)
                - queries: "team offsite" (search for events containing the exact phrase "team offsite")
            Importantly, is NOT a comma separated list.

        start_date (datetime | date | null): The start time for the search range (inclusive).
        end_date (datetime | date | null): The end time for the search range (exclusive).
    """
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    conditions = []
    params = []

    if queries:
        if isinstance(queries, str):
            queries = [queries]
        query_conditions = []
        for query in queries:
            for exact in query.split('"'):
                for tok in exact.split():
                    tok = tok.replace("*", "%")
                    query_conditions.append("(title LIKE ? OR description LIKE ?)")
                    params.extend([f"%{tok}%", f"%{tok}%"])
        conditions.append(f"({' OR '.join(query_conditions)})")

    if start_date:
        conditions.append("start_time >= ?")
        params.append(start_date)
    if end_date:
        conditions.append("end_time < ?")
        params.append(end_date)

    where_clause = " AND ".join(conditions)
    if where_clause:
        where_clause = " WHERE " + where_clause
    cursor.execute(
        f"SELECT title, description, start_time, end_time FROM calendar_events{where_clause}",
        params,
    )
    results = cursor.fetchall()
    conn.close()
    keys = [column[0] for column in cursor.description]
    return {
        "results": [dict(zip(keys, row)) for row in results],
        "count": len(results),
    }


@tool
async def create_calendar_event(
    title: str, description: str, start_time: datetime, end_time: datetime
) -> str:
    """Create a calendar event with the given details."""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()

    cursor.execute(
        "INSERT INTO calendar_events (title, description, start_time, end_time) VALUES (?, ?, ?, ?)",
        (title, description, start_time, end_time),
    )
    conn.commit()
    conn.close()
    return f"Calendar event created: {title} from {start_time} to {end_time}."


@tool
async def send_email(
    to: str, subject: str, body: str, thread_id: Optional[int] = None
) -> str:
    """Send an email with the given details."""
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    config = ensure_config()
    user_id = config["configurable"]["user_id"]
    if thread_id is None:
        # Fetch the maximum thread ID and increment it by 1
        cursor.execute("SELECT COALESCE(MAX(thread_id), 0) + 1 FROM emails")
        thread_id = cursor.fetchone()[0]
    cursor.execute(
        "INSERT INTO emails (sender, recipient, subject, body, thread_id) VALUES (?, ?, ?, ?, ?)",
        (user_id, to, subject, body, thread_id),
    )
    conn.commit()
    conn.close()
    return f"Email sent to {to} with subject '{subject}'."


## Now define graph

from typing import Annotated, Literal

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain_core.runnables import Runnable, RunnableConfig, RunnableLambda
from langchain_openai import ChatOpenAI
from typing_extensions import TypedDict

from langgraph.graph import END, StateGraph
from langgraph.graph.message import AnyMessage, add_messages
from langgraph.prebuilt import ToolNode, tools_condition


def update_dialog_stack(
    left: list[Literal["assistant", "writer"]],
    right: Optional[Literal["assistant", "writer", "pop"]],
) -> list[Literal["assistant", "writer"]]:
    if right is None:
        return left
    if right == "pop":
        return left[:-1]
    return left + [right]


class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    dialog_state: Annotated[list[Literal["assistant", "writer"]], update_dialog_stack]


# model_name = "claude-3-haiku-20240307"
# model_name = "claude-3-sonnet-20240229"
# model_name = "claude-3-opus-20240229"
# llm = ChatAnthropic(model=model_name)
llm = ChatOpenAI(model="gpt-4-turbo")

safe_tools = [search_emails, search_calendar_events]
sensitive_tools = [create_calendar_event, send_email]
sensitive_tool_names = {t.name for t in sensitive_tools}


class WriteAssistant(BaseModel):
    f"""Transfers work to an expert assistant with access to the following tools: {', '.join(list(sensitive_tool_names))}.
    
    If you invoke this tool, refrain from using any other tools until the writing assistant has completed the task."""

    request: str = Field(
        description="Any necessary followup questions the writing assistant should clarify before proceeding."
    )


prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a proactive and helpful personal assistant. "
            "Utilize the available tools to search for information and perform actions to assist the user effectively. "
            "For straightforward queries, directly respond or use the tools to find the most relevant information and provide a concise answer. "
            "If you don't know the answer immediately, query the available tools to find the information. Rather than saying something like 'it looks like there are no details about X', actually "
            "double check your bases to ensure you're not missing anything. "
            "If the user's request requires actions like sending emails, creating calendar events, or writing content, immediately delegate these tasks to the {delegate_name} tool for optimal results."
            "\nWhen searching emails or calendars to find relevant information for the user's query, do so proactively without asking for permission each time. "
            " If you don't know the answer, search first to save the user's time. "
            "\nIf your tool calls return an error or your search returns empty, try alternative methods or rephrase your search before concluding that the information is unavailable. "
            "\n<user_info>\n{user_info}\n</user_info>\n"
            "<current_time>{time}</current_time>",
        ),
        ("placeholder", "{messages}"),
    ]
).partial(time=lambda: datetime.now(), delegate_name=WriteAssistant.__name__)


tools = safe_tools + [WriteAssistant]
assistant_runnable = prompt | llm.bind_tools(tools).with_retry()

followup_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a helpful personal assistant. "
            "You provide responses by applying your learned reasoning to the informational tools at your disposal, "
            "and you inform the user when this limitation is relevant to their query, asking follow-up questions where necessary when taking actions. "
            "For simple questions, provide concise answers; for more complex inquiries, offer thorough explanations. "
            "You assist with a wide range of tasks, including email writing, scheduling, research, analysis, math, coding, and other personal matters."
            "\nWhen seeking information to answer a query, feel free to use available tools without requiring user confirmation. "
            "For example, if the user asks, 'What's the weather forecast for tomorrow?', you can directly use a weather API or search tool to find the information and provide a concise answer."
            "\nHowever, for actions that could impact the user's personal data or external services, like sending emails or creating calendar events, always ask for explicit confirmation before proceeding. "
            "For instance, if the user requests, 'Please schedule a meeting with John for next Tuesday at 2 PM,' respond with something like: 'How would you like to name your calendar event with John on [date] at 2 PM?'"
            "\nSimilarly, if the user says, 'Send an email to my boss letting her know I'll be out of the office next week,' reply with: 'Certainly. To ensure I have the correct information, could you please provide your boss's email address and confirm the specific dates you'll be out of the office? I'll send you the draft for approval before sending it to your boss.'"
            "\nIf tools error out or searches return empty, try alternate methods. Break down complex tasks into manageable steps and seek the user's approval for your proposed plan."
            "\nIf the user asks you to use a tool that you don't have access to, Escalate the task to the main assistant, who can re-route the dialog based on the user's needs."
            "Communicate in a friendly, professional manner, adapting your tone to the user's preferences. "
            "When offering facts, opinions, or recommendations, cite reputable sources to support your statements when possible."
            "<user_info>\n{user_info}\n</user_info>\n"
            "\n<current_time>{time}</current_time>",
        ),
        ("placeholder", "{messages}"),
    ]
).partial(time=lambda: datetime.now())


class CompleteOrEscalate(BaseModel):
    """A tool to mark the current task as completed and/or to escalate control of the dialog to the main assistant,
    who can re-route the dialog based on the user's needs."""

    cancel: bool = True
    reason: str

    class Config:
        schema_extra = {
            "example": {
                "cancel": True,
                "reason": "User changed their mind about the current task.",
            },
            "example 2": {
                "cancel": True,
                "reason": "I have fully completed the task.",
            },
            "example 3": {
                "cancel": False,
                "reason": "I need to search the user's emails or calendar for more information.",
            },
        }


followup_runnable = (
    followup_prompt
    | llm.bind_tools([CompleteOrEscalate] + sensitive_tools).with_retry()
)


class Assistant:
    def __init__(self, runnable: Runnable):
        self.runnable = runnable

    async def acall(self, state: State, config: RunnableConfig):
        user_id = config["configurable"]["user_id"]
        return {
            "messages": await self.runnable.ainvoke(
                {**state, "user_info": f"<user_id>{user_id}</user_id>"}
            )
        }


async def handle_tool_error(state: State) -> dict:
    error = state.get("error")
    tool_calls = state["messages"][-1].tool_calls
    return {
        "messages": [
            ToolMessage(
                content=f"Error: {repr(error)}\n please fix your mistakes.",
                tool_call_id=tc["id"],
            )
            for tc in tool_calls
        ]
    }


safe_tool_node = ToolNode(safe_tools)
sensitive_tool_node = ToolNode(sensitive_tools)

from langgraph.checkpoint.aiosqlite import AsyncSqliteSaver

builder = StateGraph(State)


def pick_starting_point(state: State) -> str:
    dialog_state = state.get("dialog_state")
    if not dialog_state:
        return "assistant"
    return dialog_state[-1]


# You can start at any of the "scoped" assistants
builder.set_conditional_entry_point(pick_starting_point)

# First, define the assistant
builder.add_node("assistant", Assistant(assistant_runnable).acall)
builder.add_node(
    "assistant_tools",
    safe_tool_node.with_fallbacks(
        [RunnableLambda(handle_tool_error)], exception_key="error"
    ),
)


def route_assistant(state):
    route = tools_condition(state)
    if route == END:
        return END
    tool_calls = state["messages"][-1].tool_calls
    if tool_calls and tool_calls[0]["name"] == WriteAssistant.__name__:  # noqa
        return "enter_write_sequence"
    return "assistant_tools"


builder.add_conditional_edges(
    "assistant",
    route_assistant,
    {
        "enter_write_sequence": "enter_write_sequence",
        "assistant_tools": "assistant_tools",
        END: END,
    },
)
builder.add_edge("assistant_tools", "assistant")


# Now define the writing sequence / assistant
# First a couple utility nodes
async def enter_write_sequence(state: State) -> dict:
    """Push the dialog stack and delegate work to the writing assistant."""
    tool_call_id = state["messages"][-1].tool_calls[0]["id"]
    return {
        "messages": [
            # The AI endpoints expect a tool response immediately after the tool call
            ToolMessage(
                content="Delegating work to the writing assistant. Use your provided tools to assist the user with their request."
                " Once you have enough information, perform the necessary actions to complete the task."
                " Note that you will see other tools used in the conversation. Remember you only have access to the tools provided in the prompt.",
                tool_call_id=tool_call_id,
            )
        ],
        "dialog_state": "write_assistant",
    }


async def pop_dialog_state(state: State) -> dict:
    """Pop the dialog stack and return to the main assistant."""
    messages = []
    if state["messages"][-1].tool_calls:
        messages.append(
            ToolMessage(
                content="Resuming dialog with the assistant. Please reflect on the past conversation and assist the user as needed.",
                tool_call_id=state["messages"][-1].tool_calls[0]["id"],
            )
        )
    return {
        "dialog_state": "pop",
        "messages": messages,
    }


## Now add the nodes to the graph'
builder.add_node("enter_write_sequence", enter_write_sequence)
builder.add_node("write_assistant", Assistant(followup_runnable).acall)
builder.add_edge("enter_write_sequence", "write_assistant")
builder.add_node(
    "writer_sensitive_tools",
    sensitive_tool_node.with_fallbacks(
        [RunnableLambda(handle_tool_error)], exception_key="error"
    ),
)
builder.add_node("leave_write_sequence", pop_dialog_state)


def route_writer(state):
    route = tools_condition(state)
    if route == END:
        return END
    tool_calls = state["messages"][-1].tool_calls
    did_cancel = any(tc["name"] == CompleteOrEscalate.__name__ for tc in tool_calls)
    if did_cancel:
        return "leave_write_sequence"
    return "writer_sensitive_tools"


builder.add_conditional_edges(
    "write_assistant",
    route_writer,
    {
        "writer_sensitive_tools": "writer_sensitive_tools",
        "leave_write_sequence": "leave_write_sequence",
        END: END,
    },
)
builder.add_edge("writer_sensitive_tools", "write_assistant")
builder.add_edge("leave_write_sequence", "assistant")


memory = AsyncSqliteSaver.from_conn_string(":memory:")
graph = builder.compile(checkpointer=memory)
