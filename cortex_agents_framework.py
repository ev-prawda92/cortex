"""
CORTEX Agent Framework
Real, runnable agents using the Anthropic API with defined tool stubs for integrations.
"""

import anthropic
import json
from typing import Optional
from abc import ABC, abstractmethod
from datetime import datetime


class CORTEXAgent(ABC):
    """Base class for all CORTEX healthcare agents."""

    def __init__(self, api_key: str, agent_name: str, agent_description: str):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.agent_name = agent_name
        self.agent_description = agent_description
        self.model = "claude-opus-4-8"

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt for this agent."""
        pass

    @abstractmethod
    def get_tools(self) -> list:
        """Return the list of tools this agent can use."""
        pass

    @abstractmethod
    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """
        Process a tool call and return the result.
        Subclasses implement actual tool logic (or stubs for integrations).
        """
        pass

    def run(self, user_message: str, max_iterations: int = 10) -> str:
        """
        Run the agent with the given input message.
        Implements the agentic loop with tool use.
        """
        messages = [{"role": "user", "content": user_message}]

        for iteration in range(max_iterations):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=self.get_system_prompt(),
                tools=self.get_tools(),
                messages=messages,
            )

            # If the agent is done (stop_reason is "end_turn"), return the response
            if response.stop_reason == "end_turn":
                return self._extract_text(response.content)

            # If tool use is requested, process it
            if response.stop_reason == "tool_use":
                # Add assistant's response to message history
                messages.append({"role": "assistant", "content": response.content})

                # Process each tool call
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        tool_result = self.process_tool_call(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": tool_result,
                        })

                # Add tool results to message history
                messages.append({"role": "user", "content": tool_results})
            else:
                # Unexpected stop reason
                break

        return "Agent reached max iterations without completion."

    def _extract_text(self, content) -> str:
        """Extract text from response content blocks."""
        for block in content:
            if hasattr(block, "text"):
                return block.text
        return ""


class NoShowOutreachAgent(CORTEXAgent):
    """Agent that proactively reaches out to patients who are likely to no-show."""

    def __init__(self, api_key: str):
        super().__init__(
            api_key,
            "No-Show Outreach",
            "Identifies at-risk patients and sends proactive outreach"
        )

    def get_system_prompt(self) -> str:
        return """You are the No-Show Outreach agent for a healthcare system. Your job is to:
1. Identify patients who are likely to miss appointments based on their history
2. Compose personalized, empathetic outreach messages
3. Suggest the best timing and channel for contact
4. Track engagement and escalate if a patient doesn't respond

You have access to patient data, appointment history, and communication preferences.
Be proactive, clear, and helpful. Format any outreach message as clear, professional text."""

    def get_tools(self) -> list:
        return [
            {
                "name": "get_at_risk_patients",
                "description": "Get list of patients with high no-show risk based on historical patterns",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "risk_threshold": {
                            "type": "number",
                            "description": "Risk score threshold (0-100). Return patients above this threshold."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of patients to return"
                        }
                    },
                    "required": ["risk_threshold"]
                }
            },
            {
                "name": "get_patient_details",
                "description": "Get detailed info about a patient including appointment and contact preferences",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient ID"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "send_outreach_message",
                "description": "Send a personalized outreach message to a patient via their preferred channel",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string"},
                        "message": {"type": "string"},
                        "channel": {
                            "type": "string",
                            "enum": ["sms", "email", "phone_call"],
                            "description": "Communication channel"
                        }
                    },
                    "required": ["patient_id", "message", "channel"]
                }
            },
            {
                "name": "log_outreach_attempt",
                "description": "Log the outreach attempt for tracking and audit",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["sent", "delivered", "failed", "no_contact_info"],
                            "description": "Status of the outreach"
                        },
                        "notes": {"type": "string"}
                    },
                    "required": ["patient_id", "status"]
                }
            }
        ]

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        """Process tool calls with mock data."""
        if tool_name == "get_at_risk_patients":
            # Mock patient data with no-show risk
            risk_threshold = tool_input.get("risk_threshold", 70)
            limit = tool_input.get("limit", 5)

            mock_patients = [
                {"patient_id": "P001", "name": "John Smith", "no_show_risk": 85, "upcoming_appointment": "2026-08-30 10:00 AM"},
                {"patient_id": "P002", "name": "Maria Garcia", "no_show_risk": 78, "upcoming_appointment": "2026-08-30 2:00 PM"},
                {"patient_id": "P003", "name": "Robert Chen", "no_show_risk": 92, "upcoming_appointment": "2026-08-31 9:00 AM"},
                {"patient_id": "P004", "name": "Lisa Johnson", "no_show_risk": 81, "upcoming_appointment": "2026-08-31 3:30 PM"},
            ]

            filtered = [p for p in mock_patients if p["no_show_risk"] >= risk_threshold][:limit]
            return json.dumps(filtered)

        elif tool_name == "get_patient_details":
            patient_id = tool_input.get("patient_id")
            # Mock patient details
            details = {
                "P001": {
                    "name": "John Smith",
                    "phone": "555-0101",
                    "email": "john.smith@email.com",
                    "preferred_contact": "sms",
                    "appointment": {"date": "2026-08-30", "time": "10:00 AM", "provider": "Dr. Williams", "reason": "Follow-up Cardiology"},
                    "past_no_shows": 3,
                    "transportation_barrier": True,
                    "language": "English"
                },
                "P002": {
                    "name": "Maria Garcia",
                    "phone": "555-0102",
                    "email": "m.garcia@email.com",
                    "preferred_contact": "email",
                    "appointment": {"date": "2026-08-30", "time": "2:00 PM", "provider": "Dr. Patel", "reason": "Lab review"},
                    "past_no_shows": 2,
                    "transportation_barrier": False,
                    "language": "Spanish"
                },
            }
            return json.dumps(details.get(patient_id, {"error": "Patient not found"}))

        elif tool_name == "send_outreach_message":
            patient_id = tool_input.get("patient_id")
            channel = tool_input.get("channel")
            # Simulate sending a message
            return json.dumps({
                "status": "sent",
                "patient_id": patient_id,
                "channel": channel,
                "timestamp": datetime.now().isoformat(),
                "message_id": f"MSG-{patient_id}-{datetime.now().timestamp()}"
            })

        elif tool_name == "log_outreach_attempt":
            patient_id = tool_input.get("patient_id")
            status = tool_input.get("status")
            return json.dumps({
                "logged": True,
                "patient_id": patient_id,
                "status": status,
                "timestamp": datetime.now().isoformat()
            })

        return json.dumps({"error": "Unknown tool"})


class AppointmentReminderAgent(CORTEXAgent):
    """Agent that sends appointment reminders to patients."""

    def __init__(self, api_key: str):
        super().__init__(
            api_key,
            "Appointment Reminder",
            "Sends timely appointment reminders to patients"
        )

    def get_system_prompt(self) -> str:
        return """You are the Appointment Reminder agent. Your responsibilities are:
1. Identify upcoming appointments (within 24-48 hours)
2. Compose clear, concise reminder messages with essential details
3. Send reminders through patient's preferred communication channel
4. Log all reminder attempts for compliance

Keep messages brief but complete. Include: date, time, provider name, location, and how to reschedule."""

    def get_tools(self) -> list:
        return [
            {
                "name": "get_upcoming_appointments",
                "description": "Get appointments scheduled for the next 24-48 hours",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "hours_ahead": {
                            "type": "integer",
                            "description": "How many hours ahead to look (default 48)"
                        }
                    }
                }
            },
            {
                "name": "get_appointment_details",
                "description": "Get full details for a specific appointment",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {"type": "string"}
                    },
                    "required": ["appointment_id"]
                }
            },
            {
                "name": "send_reminder",
                "description": "Send reminder message to patient",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string"},
                        "appointment_id": {"type": "string"},
                        "message": {"type": "string"},
                        "channel": {
                            "type": "string",
                            "enum": ["sms", "email", "both"]
                        }
                    },
                    "required": ["patient_id", "appointment_id", "message", "channel"]
                }
            }
        ]

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_upcoming_appointments":
            # Mock upcoming appointments
            return json.dumps([
                {
                    "appointment_id": "APT001",
                    "patient_id": "P001",
                    "patient_name": "John Smith",
                    "date": "2026-08-27",
                    "time": "10:00 AM",
                    "provider": "Dr. Williams",
                    "location": "Clinic A, Room 205"
                },
                {
                    "appointment_id": "APT002",
                    "patient_id": "P002",
                    "patient_name": "Maria Garcia",
                    "date": "2026-08-27",
                    "time": "2:30 PM",
                    "provider": "Dr. Patel",
                    "location": "Clinic B, Room 101"
                }
            ])

        elif tool_name == "get_appointment_details":
            appt_id = tool_input.get("appointment_id")
            details = {
                "APT001": {
                    "appointment_id": "APT001",
                    "patient_id": "P001",
                    "patient_name": "John Smith",
                    "phone": "555-0101",
                    "email": "john.smith@email.com",
                    "date": "2026-08-27",
                    "time": "10:00 AM",
                    "provider": "Dr. Williams",
                    "reason": "Follow-up Cardiology",
                    "location": "Clinic A, Room 205",
                    "duration_minutes": 30,
                    "visit_type": "in_person"
                }
            }
            return json.dumps(details.get(appt_id, {"error": "Not found"}))

        elif tool_name == "send_reminder":
            return json.dumps({
                "sent": True,
                "appointment_id": tool_input.get("appointment_id"),
                "timestamp": datetime.now().isoformat()
            })

        return json.dumps({"error": "Unknown tool"})


class LabResultNotificationAgent(CORTEXAgent):
    """Agent that notifies patients of lab results with clinical context."""

    def __init__(self, api_key: str):
        super().__init__(
            api_key,
            "Lab Result Notification",
            "Delivers lab results with appropriate clinical guidance"
        )

    def get_system_prompt(self) -> str:
        return """You are the Lab Result Notification agent. You:
1. Check for newly available lab results
2. Flag any abnormal or critical values
3. Compose clear notifications with appropriate clinical context
4. Route critical results immediately for provider review
5. Advise patients on next steps based on result severity

Always be accurate about result interpretation. For abnormal values, include:
- What the result means in simple terms
- Whether action is needed
- Who to contact if concerned"""

    def get_tools(self) -> list:
        return [
            {
                "name": "get_new_lab_results",
                "description": "Retrieve newly available lab results awaiting notification",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer"}
                    }
                }
            },
            {
                "name": "get_result_details",
                "description": "Get full details and clinical context for a lab result",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "result_id": {"type": "string"}
                    },
                    "required": ["result_id"]
                }
            },
            {
                "name": "check_critical_values",
                "description": "Check if a result indicates critical/dangerous values requiring immediate escalation",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "result_id": {"type": "string"}
                    },
                    "required": ["result_id"]
                }
            },
            {
                "name": "notify_patient",
                "description": "Send result notification to patient",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string"},
                        "result_id": {"type": "string"},
                        "message": {"type": "string"},
                        "severity": {
                            "type": "string",
                            "enum": ["normal", "abnormal", "critical"]
                        }
                    },
                    "required": ["patient_id", "result_id", "message", "severity"]
                }
            },
            {
                "name": "escalate_to_provider",
                "description": "Escalate critical result to provider for immediate review",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "result_id": {"type": "string"},
                        "reason": {"type": "string"}
                    },
                    "required": ["result_id", "reason"]
                }
            }
        ]

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_new_lab_results":
            return json.dumps([
                {
                    "result_id": "LAB001",
                    "patient_id": "P001",
                    "test_type": "Blood Work - CBC",
                    "order_date": "2026-08-25",
                    "result_date": "2026-08-26",
                    "status": "pending_notification"
                },
                {
                    "result_id": "LAB002",
                    "patient_id": "P003",
                    "test_type": "Lipid Panel",
                    "order_date": "2026-08-24",
                    "result_date": "2026-08-26",
                    "status": "pending_notification"
                }
            ])

        elif tool_name == "get_result_details":
            result_id = tool_input.get("result_id")
            details = {
                "LAB001": {
                    "result_id": "LAB001",
                    "patient_id": "P001",
                    "patient_name": "John Smith",
                    "test_type": "Blood Work - CBC",
                    "values": {
                        "WBC": {"value": 7.2, "unit": "K/uL", "normal_range": "4.5-11.0", "status": "normal"},
                        "RBC": {"value": 4.9, "unit": "M/uL", "normal_range": "4.5-5.9", "status": "normal"},
                        "Hemoglobin": {"value": 14.5, "unit": "g/dL", "normal_range": "13.5-17.5", "status": "normal"}
                    },
                    "provider": "Dr. Williams",
                    "interpretation": "All values within normal limits"
                },
                "LAB002": {
                    "result_id": "LAB002",
                    "patient_id": "P003",
                    "patient_name": "Robert Chen",
                    "test_type": "Lipid Panel",
                    "values": {
                        "Total Cholesterol": {"value": 245, "unit": "mg/dL", "normal_range": "<200", "status": "abnormal_high"},
                        "LDL": {"value": 165, "unit": "mg/dL", "normal_range": "<100", "status": "abnormal_high"},
                        "HDL": {"value": 35, "unit": "mg/dL", "normal_range": ">40", "status": "abnormal_low"}
                    },
                    "provider": "Dr. Patel",
                    "interpretation": "Elevated cholesterol and LDL. Low HDL. Recommend follow-up."
                }
            }
            return json.dumps(details.get(result_id, {"error": "Not found"}))

        elif tool_name == "check_critical_values":
            result_id = tool_input.get("result_id")
            criticals = {
                "LAB001": {"is_critical": False, "values": []},
                "LAB002": {"is_critical": False, "reason": "Abnormal but not immediately life-threatening"}
            }
            return json.dumps(criticals.get(result_id, {"is_critical": False}))

        elif tool_name == "notify_patient":
            return json.dumps({
                "notified": True,
                "result_id": tool_input.get("result_id"),
                "timestamp": datetime.now().isoformat()
            })

        elif tool_name == "escalate_to_provider":
            return json.dumps({
                "escalated": True,
                "result_id": tool_input.get("result_id"),
                "timestamp": datetime.now().isoformat()
            })

        return json.dumps({"error": "Unknown tool"})


class PriorAuthAgent(CORTEXAgent):
    """Agent that manages prior authorization requests."""

    def __init__(self, api_key: str):
        super().__init__(
            api_key,
            "Prior Auth (Insurance)",
            "Manages prior authorization requests with insurance payers"
        )

    def get_system_prompt(self) -> str:
        return """You are the Prior Authorization agent. You handle:
1. Receiving prior auth requests from clinical staff
2. Gathering necessary clinical documentation
3. Formatting requests according to payer requirements
4. Submitting to insurance systems
5. Tracking status and following up
6. Notifying providers of approval/denial

You understand insurance rules, medical necessity criteria, and payer-specific requirements.
Be thorough but efficient. Always document everything for audit trail."""

    def get_tools(self) -> list:
        return [
            {
                "name": "get_pending_auth_requests",
                "description": "Retrieve prior auth requests pending submission",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "payer": {
                            "type": "string",
                            "enum": ["Blue Shield", "Aetna", "UnitedHealth", "Cigna"]
                        },
                        "status": {
                            "type": "string",
                            "enum": ["draft", "pending_review", "ready_to_submit"]
                        }
                    }
                }
            },
            {
                "name": "get_clinical_documentation",
                "description": "Retrieve clinical notes and documentation for a patient's requested service",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string"},
                        "service_code": {"type": "string"},
                        "request_id": {"type": "string"}
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "format_payer_request",
                "description": "Format prior auth request according to specific payer requirements",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "payer": {"type": "string"},
                        "patient_id": {"type": "string"},
                        "service_description": {"type": "string"}
                    },
                    "required": ["request_id", "payer", "patient_id"]
                }
            },
            {
                "name": "submit_to_payer",
                "description": "Submit formatted prior auth request to insurance payer system (integration point)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "payer": {"type": "string"},
                        "formatted_request": {"type": "string"}
                    },
                    "required": ["request_id", "payer"]
                }
            },
            {
                "name": "check_auth_status",
                "description": "Check status of previously submitted prior authorization",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "payer_reference_id": {"type": "string"}
                    }
                }
            },
            {
                "name": "notify_provider_and_patient",
                "description": "Notify provider and patient of authorization outcome",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "request_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["approved", "denied", "pending", "needs_more_info"]
                        },
                        "details": {"type": "string"}
                    },
                    "required": ["request_id", "status"]
                }
            }
        ]

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_pending_auth_requests":
            return json.dumps([
                {
                    "request_id": "AUTH001",
                    "patient_id": "P002",
                    "patient_name": "Maria Garcia",
                    "payer": "Blue Shield",
                    "service": "MRI Brain without contrast",
                    "status": "pending_review",
                    "created_date": "2026-08-24"
                },
                {
                    "request_id": "AUTH002",
                    "patient_id": "P004",
                    "patient_name": "Lisa Johnson",
                    "payer": "Aetna",
                    "service": "Physical Therapy (20 sessions)",
                    "status": "draft",
                    "created_date": "2026-08-25"
                }
            ])

        elif tool_name == "get_clinical_documentation":
            patient_id = tool_input.get("patient_id")
            docs = {
                "P002": {
                    "patient_id": "P002",
                    "provider_notes": "Patient with new onset headaches, suspect migraine. MRI ordered to rule out structural lesions.",
                    "diagnosis": "Headache, unspecified",
                    "icd10": "R51.9",
                    "recent_exams": ["Physical exam 2026-08-24: Neurologically intact"],
                    "medical_necessity": "High - rules out serious pathology before starting migraine prophylaxis"
                }
            }
            return json.dumps(docs.get(patient_id, {"error": "No documentation found"}))

        elif tool_name == "format_payer_request":
            payer = tool_input.get("payer")
            # Show that different payers have different requirements
            payer_formats = {
                "Blue Shield": {
                    "format_version": "EDI 837",
                    "required_fields": ["member_id", "service_code", "medical_necessity", "provider_npi"],
                    "status": "formatted"
                },
                "Aetna": {
                    "format_version": "Aetna Portal XML",
                    "required_fields": ["case_id", "service_description", "clinical_justification"],
                    "status": "formatted"
                }
            }
            return json.dumps(payer_formats.get(payer, {"error": "Unknown payer format"}))

        elif tool_name == "submit_to_payer":
            # This is where the actual integration would happen
            return json.dumps({
                "submitted": True,
                "request_id": tool_input.get("request_id"),
                "payer": tool_input.get("payer"),
                "payer_reference_id": f"PAY-{datetime.now().timestamp()}",
                "submission_timestamp": datetime.now().isoformat(),
                "note": "[INTEGRATION POINT] In production, this would call the payer's API/EDI submission system"
            })

        elif tool_name == "check_auth_status":
            # Mock status check
            statuses = {
                "AUTH001": {"status": "pending", "days_waiting": 2, "expected_response": "2026-08-28"},
                "AUTH002": {"status": "more_info_needed", "request": "Details on PT provider credentials"}
            }
            ref_id = tool_input.get("payer_reference_id")
            return json.dumps({"status": "pending", "last_checked": datetime.now().isoformat()})

        elif tool_name == "notify_provider_and_patient":
            return json.dumps({
                "notified": True,
                "request_id": tool_input.get("request_id"),
                "timestamp": datetime.now().isoformat()
            })

        return json.dumps({"error": "Unknown tool"})


if __name__ == "__main__":
    print("CORTEX Agent Framework loaded successfully.")
    print("See demo_agents.py for example usage.")


class IntegrationAssistantAgent(CORTEXAgent):
    """Agent that helps developers integrate CORTEX agents into their applications."""

    def __init__(self, api_key: str):
        super().__init__(api_key, "Integration Assistant", "Help developers integrate CORTEX into their apps")
        self.model = "claude-3-5-sonnet-20241022"

    def get_system_prompt(self) -> str:
        return """You are an expert integration engineer helping developers embed CORTEX healthcare agents into their applications.

Your role:
1. Answer technical questions about CORTEX SDK integration
2. Guide developers through authentication and API key setup
3. Help with webhook configuration and event handling
4. Provide working code examples (JavaScript, Python, etc.)
5. Troubleshoot common integration issues
6. Recommend best practices for production deployment

You have knowledge of:
- CORTEX SDK installation and setup
- Multi-provider support (Anthropic, OpenAI, Google Gemini)
- REST API endpoints and authentication
- Webhook payload structures and verification
- Rate limiting and error handling
- HIPAA compliance for healthcare integrations
- Common gotchas and how to avoid them

When a developer asks:
- Clarify their use case and environment
- Provide step-by-step guidance
- Include ready-to-use code snippets
- Suggest security best practices
- Point out potential issues before they happen

Always be encouraging and assume they may be new to healthcare integrations."""

    def get_tools(self) -> list:
        return [
            {
                "name": "get_sdk_documentation",
                "description": "Retrieve current SDK documentation and code examples",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "Topic to look up: installation, authentication, webhooks, examples, error_handling, etc."
                        }
                    },
                    "required": ["topic"]
                }
            },
            {
                "name": "get_code_example",
                "description": "Provide a working code example for a specific integration task",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "language": {"type": "string", "description": "JavaScript, Python, Go, etc."},
                        "task": {"type": "string", "description": "What the code should do: setup, auth, webhook, run-agent, etc."}
                    },
                    "required": ["language", "task"]
                }
            },
            {
                "name": "check_error",
                "description": "Help diagnose and resolve integration errors",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "error_message": {"type": "string", "description": "The full error message"},
                        "context": {"type": "string", "description": "What were you trying to do?"}
                    },
                    "required": ["error_message"]
                }
            }
        ]

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_sdk_documentation":
            topic = tool_input.get("topic", "general").lower()
            docs = {
                "installation": "SDK Installation: npm install @cortex/sdk or pip install cortex-sdk",
                "authentication": "API Key setup: Store in environment variable CORTEX_API_KEY, never in client code",
                "webhooks": "Webhook events: agent.run.started, agent.run.completed, agent.escalated",
                "examples": "See https://github.com/cortex-health/sdk-examples for working projects",
                "error_handling": "Always catch fetch errors and implement exponential backoff retry logic",
                "security": "Use HTTPS, verify webhook signatures with HMAC-SHA256, rotate API keys quarterly"
            }
            return json.dumps({
                "topic": topic,
                "documentation": docs.get(topic, "See integration docs at https://docs.cortex.health")
            })
        
        elif tool_name == "get_code_example":
            language = tool_input.get("language", "javascript").lower()
            task = tool_input.get("task", "setup").lower()
            
            examples = {
                ("javascript", "setup"): """const cortex = new Cortex({
  siteId: 'your-site-id',
  apiKey: process.env.CORTEX_API_KEY,
  agentId: 'no-show',
  endpoint: 'https://cortex.your-org.com/api'
});
cortex.mount('#cortex-widget');""",
                ("python", "setup"): """import requests
cortex = Cortex(
    site_id='your-site-id',
    api_key=os.environ['CORTEX_API_KEY'],
    agent_id='no-show',
    endpoint='https://cortex.your-org.com/api'
)""",
                ("javascript", "webhook"): """app.post('/webhooks/cortex', (req, res) => {
  const event = req.body;
  if (event.event === 'agent.run.completed') {
    console.log(`Agent ${event.agent_id} completed in ${event.steps} steps`);
    if (event.escalated) notifyTeam(event);
  }
  res.status(200).send('ok');
});"""
            }
            
            key = (language, task)
            code = examples.get(key, "Example not available - check documentation")
            return json.dumps({"language": language, "task": task, "code": code})
        
        elif tool_name == "check_error":
            error = tool_input.get("error_message", "").lower()
            context = tool_input.get("context", "")
            
            if "401" in error or "unauthorized" in error:
                return json.dumps({"diagnosis": "API key is invalid or missing", "fix": "Check CORTEX_API_KEY environment variable"})
            elif "404" in error or "not found" in error:
                return json.dumps({"diagnosis": "Endpoint or resource not found", "fix": "Verify agent_id and endpoint URL"})
            elif "429" in error or "rate limit" in error:
                return json.dumps({"diagnosis": "Rate limit exceeded", "fix": "Implement exponential backoff retry logic"})
            else:
                return json.dumps({"diagnosis": "Error requires investigation", "context": context, "suggestion": "Check logs and enable debug mode"})
        
        return json.dumps({"status": "tool not found"})
