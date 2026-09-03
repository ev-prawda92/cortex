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


class AuscultAgent(CORTEXAgent):
    """
    Auscult — the adjudication gate between ambient AI scribes and EHR systems.
    Parses clinical encounter transcripts into structured order proposals, runs
    deterministic safety checks (drug-allergy, dose-range, drug-drug interactions),
    and routes each proposal through Cortex's approval gate for physician attestation
    before any chart action is written.
    """

    def __init__(self, api_key: str):
        super().__init__(
            api_key,
            "Auscult",
            "Clinical voice-to-chart adjudication agent — transcript in, attested orders out"
        )
        # Auscult uses a reasoning-capable model for medical transcript parsing
        self.model = "claude-opus-4-8"

        # ── Mock chart context (EHR integration point) ──────────────────
        self._chart_db = {
            "P-10442": {
                "patient_id": "P-10442",
                "name": "Margaret Reeves",
                "age": 67,
                "sex": "F",
                "allergies": [
                    {"substance": "amoxicillin", "reaction": "anaphylaxis", "severity": "severe"},
                    {"substance": "sulfa", "reaction": "rash", "severity": "moderate"}
                ],
                "active_meds": [
                    {"name": "lisinopril", "dose": "10mg", "frequency": "daily", "class": "ACE inhibitor"},
                    {"name": "atorvastatin", "dose": "40mg", "frequency": "daily", "class": "statin"},
                    {"name": "metformin", "dose": "500mg", "frequency": "BID", "class": "biguanide"}
                ],
                "recent_labs": [
                    {"test": "BMP", "date": "2026-07-15", "results": {"potassium": 4.2, "creatinine": 0.9, "eGFR": 78}},
                    {"test": "HbA1c", "date": "2026-06-01", "result": 6.8}
                ],
                "conditions": ["essential hypertension", "type 2 diabetes", "hyperlipidemia"],
                "provider": "Dr. Sarah Chen"
            }
        }

        # ── Formulary / dose-range reference (integration point) ───────
        self._formulary = {
            "lisinopril": {"class": "ACE inhibitor", "min_dose_mg": 2.5, "max_dose_mg": 40, "unit": "mg",
                           "interactions": ["potassium supplements", "spironolactone", "sacubitril"],
                           "contraindicated_allergies": []},
            "amoxicillin": {"class": "penicillin", "min_dose_mg": 250, "max_dose_mg": 3000, "unit": "mg",
                            "interactions": ["warfarin", "methotrexate"],
                            "contraindicated_allergies": ["amoxicillin", "penicillin"]},
            "clindamycin": {"class": "lincosamide", "min_dose_mg": 150, "max_dose_mg": 1800, "unit": "mg",
                            "interactions": ["erythromycin", "neuromuscular blockers"],
                            "contraindicated_allergies": ["clindamycin"]},
            "atorvastatin": {"class": "statin", "min_dose_mg": 10, "max_dose_mg": 80, "unit": "mg",
                             "interactions": ["cyclosporine", "gemfibrozil", "niacin"],
                             "contraindicated_allergies": []},
            "metformin": {"class": "biguanide", "min_dose_mg": 500, "max_dose_mg": 2550, "unit": "mg",
                          "interactions": ["contrast dye", "alcohol"],
                          "contraindicated_allergies": []}
        }

    def get_system_prompt(self) -> str:
        return """You are Auscult, a clinical adjudication agent operating inside the CORTEX platform.

Your single job: take a raw transcript from a clinical encounter and convert it into
structured, safety-checked order proposals that a physician can attest to with one action.

Pipeline you follow on every transcript:
1. PARSE — Extract every clinical action the provider stated or implied (med changes,
   lab orders, referrals, follow-ups). Output structured proposals.
2. CHART CONTEXT — Load the patient's current meds, allergies, recent labs, and conditions.
3. SAFETY CHECK — Run each proposal through deterministic checks:
   • Drug-allergy cross-reference
   • Dose-range validation against formulary
   • Drug-drug interaction screening against active meds
4. FLAG or CLEAR — Mark each proposal as SAFE, WARNING, or BLOCKED with reasons.
5. SUBMIT FOR ATTESTATION — Send the batch to Cortex's approval gate. Nothing writes
   to the chart until a physician attests.

Rules:
- You NEVER skip the safety check step, even if the transcript seems routine.
- A BLOCKED proposal must include a safer alternative when one exists.
- You always call submit_for_attestation at the end — you cannot write to a chart directly.
- Be concise in your reasoning. Clinicians scan, they don't read essays."""

    def get_tools(self) -> list:
        return [
            {
                "name": "parse_transcript",
                "description": "Parse a clinical encounter transcript into structured order proposals. Returns a list of proposed actions (medication changes, lab orders, referrals, follow-ups) with dosing details extracted from the conversation.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "transcript": {
                            "type": "string",
                            "description": "Raw text transcript of the clinical encounter"
                        },
                        "patient_id": {
                            "type": "string",
                            "description": "Patient identifier to associate proposals with"
                        }
                    },
                    "required": ["transcript", "patient_id"]
                }
            },
            {
                "name": "load_chart_context",
                "description": "Load patient chart context from the EHR — current medications, allergies, recent labs, active conditions. Required before running safety checks.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient identifier"
                        }
                    },
                    "required": ["patient_id"]
                }
            },
            {
                "name": "run_safety_checks",
                "description": "Run deterministic safety checks on a batch of proposals against patient chart context. Checks drug-allergy cross-references, dose-range validation, and drug-drug interactions. Returns each proposal marked SAFE, WARNING, or BLOCKED.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {
                            "type": "string",
                            "description": "Patient identifier (chart context must be loaded first)"
                        },
                        "proposals": {
                            "type": "array",
                            "description": "List of structured proposals to check",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "proposal_id": {"type": "string"},
                                    "type": {"type": "string", "enum": ["medication", "lab_order", "referral", "follow_up"]},
                                    "action": {"type": "string"},
                                    "medication": {"type": "string"},
                                    "dose": {"type": "string"},
                                    "details": {"type": "string"}
                                },
                                "required": ["proposal_id", "type", "action"]
                            }
                        }
                    },
                    "required": ["patient_id", "proposals"]
                }
            },
            {
                "name": "submit_for_attestation",
                "description": "Submit the safety-checked proposal batch to Cortex's approval gate for physician attestation. Creates an ApprovalRequest per proposal. Nothing writes to the chart until the physician attests.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "patient_id": {"type": "string"},
                        "provider": {"type": "string", "description": "Ordering provider name"},
                        "proposals": {
                            "type": "array",
                            "description": "Safety-checked proposals with status",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "proposal_id": {"type": "string"},
                                    "type": {"type": "string"},
                                    "action": {"type": "string"},
                                    "safety_status": {"type": "string", "enum": ["SAFE", "WARNING", "BLOCKED"]},
                                    "safety_notes": {"type": "string"},
                                    "alternative": {"type": "string"}
                                },
                                "required": ["proposal_id", "type", "action", "safety_status"]
                            }
                        }
                    },
                    "required": ["patient_id", "provider", "proposals"]
                }
            }
        ]

    def process_tool_call(self, tool_name: str, tool_input: dict) -> str:

        if tool_name == "parse_transcript":
            # In production: medical ASR + reasoning model (swappable).
            # Here we return structured proposals from the mock encounter.
            patient_id = tool_input.get("patient_id", "P-10442")
            return json.dumps({
                "patient_id": patient_id,
                "encounter_type": "office_visit",
                "proposals": [
                    {
                        "proposal_id": "RX-001",
                        "type": "medication",
                        "action": "modify",
                        "medication": "lisinopril",
                        "current_dose": "10mg daily",
                        "new_dose": "20mg daily",
                        "details": "Increase lisinopril for persistent hypertension — BP 148/92 today"
                    },
                    {
                        "proposal_id": "LAB-001",
                        "type": "lab_order",
                        "action": "order",
                        "details": "Basic Metabolic Panel — recheck potassium and renal function after ACE inhibitor dose increase",
                        "timing": "2 weeks"
                    },
                    {
                        "proposal_id": "LAB-002",
                        "type": "lab_order",
                        "action": "order",
                        "details": "Lipid panel — overdue, last drawn > 12 months ago",
                        "timing": "fasting, next visit"
                    },
                    {
                        "proposal_id": "REF-001",
                        "type": "referral",
                        "action": "order",
                        "details": "Cardiology referral — persistent uncontrolled HTN despite medication adjustment",
                        "urgency": "routine"
                    },
                    {
                        "proposal_id": "RX-002",
                        "type": "medication",
                        "action": "new",
                        "medication": "amoxicillin",
                        "dose": "500mg TID x 10 days",
                        "details": "For dental abscess noted during exam"
                    }
                ]
            })

        elif tool_name == "load_chart_context":
            patient_id = tool_input.get("patient_id")
            chart = self._chart_db.get(patient_id)
            if chart:
                return json.dumps(chart)
            return json.dumps({"error": f"No chart found for patient {patient_id}"})

        elif tool_name == "run_safety_checks":
            patient_id = tool_input.get("patient_id")
            proposals = tool_input.get("proposals", [])
            chart = self._chart_db.get(patient_id, {})
            allergies = {a["substance"].lower() for a in chart.get("allergies", [])}
            active_meds = {m["name"].lower(): m for m in chart.get("active_meds", [])}
            results = []

            for p in proposals:
                status = "SAFE"
                notes = []
                alternative = None
                med_name = (p.get("medication") or "").lower()

                if p["type"] == "medication" and med_name:
                    formulary_entry = self._formulary.get(med_name, {})

                    # ── Drug-allergy cross-check ───────────────────────
                    contra_allergies = set(formulary_entry.get("contraindicated_allergies", []))
                    allergy_hit = allergies & (contra_allergies | {med_name})
                    if allergy_hit:
                        status = "BLOCKED"
                        reactions = [a for a in chart.get("allergies", [])
                                     if a["substance"].lower() in allergy_hit]
                        reaction_str = "; ".join(
                            f"{r['substance']}: {r['reaction']} ({r['severity']})" for r in reactions
                        )
                        notes.append(f"ALLERGY CONFLICT — {reaction_str}")
                        # Suggest alternative
                        if med_name in ("amoxicillin", "penicillin"):
                            alternative = "clindamycin 300mg TID x 10 days (no cross-reactivity with penicillin allergy)"

                    # ── Dose-range validation ──────────────────────────
                    if formulary_entry and status != "BLOCKED":
                        dose_str = p.get("dose") or p.get("new_dose", "")
                        import re
                        dose_match = re.search(r'(\d+(?:\.\d+)?)\s*mg', dose_str, re.IGNORECASE)
                        if dose_match:
                            dose_val = float(dose_match.group(1))
                            min_d = formulary_entry.get("min_dose_mg", 0)
                            max_d = formulary_entry.get("max_dose_mg", 99999)
                            if dose_val > max_d:
                                status = "BLOCKED"
                                notes.append(f"DOSE EXCEEDS MAX — {dose_val}mg > formulary max {max_d}mg")
                            elif dose_val < min_d:
                                status = "WARNING"
                                notes.append(f"DOSE BELOW MIN — {dose_val}mg < formulary min {min_d}mg")
                            else:
                                notes.append(f"Dose in range ({min_d}–{max_d}mg)")

                    # ── Drug-drug interaction screening ────────────────
                    if formulary_entry and status != "BLOCKED":
                        known_interactions = set(i.lower() for i in formulary_entry.get("interactions", []))
                        active_names = set(active_meds.keys())
                        active_classes = set(m.get("class", "").lower() for m in active_meds.values())
                        interaction_hits = known_interactions & (active_names | active_classes)
                        if interaction_hits:
                            if status != "BLOCKED":
                                status = "WARNING"
                            notes.append(f"INTERACTION — with active: {', '.join(interaction_hits)}")

                if not notes:
                    notes.append("No safety concerns identified")

                results.append({
                    "proposal_id": p["proposal_id"],
                    "type": p["type"],
                    "action": p.get("action", ""),
                    "safety_status": status,
                    "safety_notes": " | ".join(notes),
                    "alternative": alternative
                })

            return json.dumps({"checked": len(results), "results": results})

        elif tool_name == "submit_for_attestation":
            # In production: creates ApprovalRequest records in Cortex DB
            # and notifies the provider via Cortex's notification system.
            patient_id = tool_input.get("patient_id")
            provider = tool_input.get("provider")
            proposals = tool_input.get("proposals", [])
            approvals_created = []

            for p in proposals:
                approval = {
                    "approval_id": f"APR-{p['proposal_id']}",
                    "agent_id": "auscult",
                    "run_id": f"auscult-run-{datetime.now().strftime('%Y%m%d%H%M%S')}",
                    "action": f"{p['type']}: {p['action']}",
                    "context": {
                        "patient_id": patient_id,
                        "provider": provider,
                        "proposal": p,
                        "safety_status": p.get("safety_status"),
                        "safety_notes": p.get("safety_notes"),
                        "alternative": p.get("alternative")
                    },
                    "status": "pending",
                    "expires_at": (datetime.now().replace(hour=23, minute=59)).isoformat(),
                    "note": "[INTEGRATION POINT] In production, creates ApprovalRequest in Cortex DB and notifies provider"
                }
                approvals_created.append(approval)

            blocked_count = sum(1 for p in proposals if p.get("safety_status") == "BLOCKED")
            warning_count = sum(1 for p in proposals if p.get("safety_status") == "WARNING")
            safe_count = sum(1 for p in proposals if p.get("safety_status") == "SAFE")

            return json.dumps({
                "submitted": True,
                "patient_id": patient_id,
                "provider": provider,
                "total_proposals": len(proposals),
                "safe": safe_count,
                "warnings": warning_count,
                "blocked": blocked_count,
                "approval_requests": approvals_created,
                "awaiting_attestation": True,
                "note": "All proposals routed through Cortex approval gate. Chart write is gated on physician attestation."
            })

        return json.dumps({"error": f"Unknown tool: {tool_name}"})
