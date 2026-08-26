"""
CORTEX Agent Demo
Run individual agents to see them in action with mock data.

Usage:
  export ANTHROPIC_API_KEY="your-api-key-here"
  python demo_agents.py
"""

import os
import sys
from cortex_agents_framework import (
    NoShowOutreachAgent,
    AppointmentReminderAgent,
    LabResultNotificationAgent,
    PriorAuthAgent
)


def print_demo_header(agent_name: str):
    print(f"\n{'='*70}")
    print(f"CORTEX Agent Demo: {agent_name}")
    print(f"{'='*70}\n")


def demo_no_show_outreach():
    """Demo: No-Show Outreach Agent"""
    print_demo_header("No-Show Outreach")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    agent = NoShowOutreachAgent(api_key)

    user_message = """
    I need you to identify patients at high risk of no-showing their appointments
    this week and send them personalized outreach. Consider their history and
    communication preferences. For each patient, compose an appropriate message and
    send it through their preferred channel.
    """

    print(f"User Request:\n{user_message}\n")
    print("Agent Response:\n")
    result = agent.run(user_message)
    print(result)


def demo_appointment_reminder():
    """Demo: Appointment Reminder Agent"""
    print_demo_header("Appointment Reminder")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    agent = AppointmentReminderAgent(api_key)

    user_message = """
    Send appointment reminders for all patients with appointments scheduled
    in the next 48 hours. Make the messages clear and include all essential details.
    """

    print(f"User Request:\n{user_message}\n")
    print("Agent Response:\n")
    result = agent.run(user_message)
    print(result)


def demo_lab_result_notification():
    """Demo: Lab Result Notification Agent"""
    print_demo_header("Lab Result Notification")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    agent = LabResultNotificationAgent(api_key)

    user_message = """
    Process all newly available lab results. For each result:
    1. Check if values are normal, abnormal, or critical
    2. Determine if immediate escalation is needed
    3. Compose an appropriate notification for the patient
    4. Escalate any critical results to the provider immediately
    """

    print(f"User Request:\n{user_message}\n")
    print("Agent Response:\n")
    result = agent.run(user_message)
    print(result)


def demo_prior_auth():
    """Demo: Prior Authorization Agent"""
    print_demo_header("Prior Authorization (Insurance)")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set")
        return

    agent = PriorAuthAgent(api_key)

    user_message = """
    Review pending prior authorization requests and prepare them for submission:
    1. Get all pending requests
    2. Gather necessary clinical documentation for each
    3. Format requests according to each payer's requirements
    4. Submit ready requests to the appropriate payer
    5. Brief summary of what was submitted
    """

    print(f"User Request:\n{user_message}\n")
    print("Agent Response:\n")
    result = agent.run(user_message)
    print(result)


def show_menu():
    """Display agent selection menu."""
    print("\n" + "="*70)
    print("CORTEX Agent Framework - Demo")
    print("="*70)
    print("\nSelect an agent to demo:")
    print("1. No-Show Outreach")
    print("2. Appointment Reminder")
    print("3. Lab Result Notification")
    print("4. Prior Authorization (Insurance)")
    print("5. Run all demos")
    print("0. Exit")
    print("\n")


def main():
    # Check for API key
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set")
        print("Please set it before running this demo:")
        print("  export ANTHROPIC_API_KEY='your-key-here'")
        sys.exit(1)

    # If no argument provided, show menu
    if len(sys.argv) < 2:
        show_menu()
        choice = input("Enter choice (0-5): ").strip()
    else:
        choice = sys.argv[1]

    if choice == "1":
        demo_no_show_outreach()
    elif choice == "2":
        demo_appointment_reminder()
    elif choice == "3":
        demo_lab_result_notification()
    elif choice == "4":
        demo_prior_auth()
    elif choice == "5":
        demo_no_show_outreach()
        demo_appointment_reminder()
        demo_lab_result_notification()
        demo_prior_auth()
    elif choice == "0":
        print("Exiting.")
    else:
        print("Invalid choice.")


if __name__ == "__main__":
    main()
