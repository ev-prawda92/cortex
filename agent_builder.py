"""Plain-text agent briefs and business starter ideas.

Creating a brief makes no model call, grants no tools, and starts no work.
The author's text is the instruction, rather than a generated approximation.
Examples are fictional; each names the data needed for live operation.
"""

from copy import deepcopy


STARTERS = (
    {
        "id": "invoice-check", "name": "Vendor invoice check",
        "summary": "Find charges that differ from agreed prices.",
        "needs": ["Vendor invoices", "Agreed price sheets or purchase orders"],
        "prompt": "Compare the invoices I provide with the agreed price sheet. Flag duplicate invoice numbers, unexpected fees, and price differences. For each finding, cite the invoice and agreement, show the billed and expected amounts, and draft a credit request for me to review. Treat missing evidence as a question; never assume an overcharge or send a message.",
        "sample": "Agreed price sheet: Northwind delivery fee is $140 per shipment. Invoice NW-104: one shipment, delivery fee $147. Invoice NW-105: one shipment, delivery fee $140.",
        "expected": "Flag NW-104 for a $7 difference and cite the agreed $140 delivery fee. NW-105 matches. Prepare a credit request for review; no message is sent.",
    },
    {
        "id": "payment-follow-up", "name": "Overdue payment follow-up",
        "summary": "Prepare the next step on unpaid customer invoices.",
        "needs": ["Customer invoices and payment status", "Previous customer correspondence"],
        "prompt": "Review the customer invoices and correspondence I provide. Identify overdue balances as of the date in the records, exclude paid invoices, and flag disputes separately. Draft a polite reminder for each undisputed overdue invoice. Show the source and amount; leave every message for my approval.",
        "sample": "As of 2026-10-10: Invoice AC-20, Acorn Studio, $600, due 2026-10-01, unpaid, no dispute. Invoice AC-21, $250, due 2026-10-15, unpaid. Invoice BR-10, $400, due 2026-10-01, paid 2026-10-03.",
        "expected": "Prepare a reminder for AC-20, which is nine days overdue. Exclude AC-21 (not due) and BR-10 (paid).",
    },
    {
        "id": "inquiry-follow-up", "name": "Missed inquiry follow-up",
        "summary": "Find unanswered inquiries and prepare replies.",
        "needs": ["Incoming inquiries", "Reply history", "Approved service information"],
        "prompt": "Read the inquiries, reply history, and approved service information I provide. Find unanswered inquiries, draft a useful reply using only the approved information, and identify anything I need to answer myself. Never invent availability or prices. Leave all replies for review.",
        "sample": "Approved information: introductory consultation is $75; availability is not provided. Inquiry Q-12 from Taylor: 'How much is a consultation, and can I come Tuesday?' No reply. Inquiry Q-13 from Morgan: 'Where are you located?' Replied yesterday.",
        "expected": "Draft a response to Q-12 with the $75 price and a request to confirm Tuesday availability. Exclude Q-13 because it has a reply.",
    },
    {
        "id": "subscription-review", "name": "Subscription review",
        "summary": "Check renewals, purchased seats, and actual usage.",
        "needs": ["Subscription contracts and renewal dates", "Seat assignments and usage records"],
        "prompt": "Review the subscription contracts and usage records I provide. Flag upcoming cancellation deadlines and unused seats when the records support that finding. Separate confirmed facts from questions. Recommend items for review; never cancel a subscription or change a plan.",
        "sample": "As of 2026-10-10: CloudDesk contract has 10 seats at $20 per seat per month. Usage export shows 6 seats active and 4 assigned seats with no activity in the last 90 days. Renewal is 2026-11-15; cancellation notice is due 30 days before renewal. Seat reductions require contract confirmation.",
        "expected": "Flag the October 16 cancellation notice deadline and four seats for review. $80/month is only a potential reduction if the contract permits it; it is not realized savings.",
    },
    {
        "id": "document-collection", "name": "Missing document check",
        "summary": "Track missing forms and prepare requests.",
        "needs": ["Required-document checklist", "Submitted documents and contacts"],
        "prompt": "Compare the checklist with the submitted documents I provide. Report missing or expired items, cite the requirement, identify the contact responsible, and draft a request for each missing item. Do not mark a requirement complete without evidence or send requests automatically.",
        "sample": "Checklist for Northwind: signed service agreement and current insurance certificate. Service agreement is signed. Insurance certificate expired 2026-09-30. As-of date is 2026-10-10. Contact: Jordan, Northwind office manager.",
        "expected": "Identify the expired insurance certificate, retain the completed agreement, and draft a request to Jordan for a current certificate.",
    },
    {
        "id": "daily-brief", "name": "Owner's daily brief",
        "summary": "Turn business records into a short action list.",
        "needs": ["Business reports", "Open tasks and their owners"],
        "prompt": "Read the business reports and task list I provide. Prepare a short brief with the three issues needing attention, the evidence for each, the responsible person, and the next action. State when data is missing. Do not invent metrics, change records, or contact anyone.",
        "sample": "Invoice NW-104 has a confirmed $7 price difference; Pat owns vendor follow-up. Inquiry Q-12 has no reply; Lee owns customer inquiries. The insurance certificate for Northwind expired September 30; Jordan is the contact. No sales report was provided.",
        "expected": "List the price difference, unanswered inquiry, and expired certificate with their owners and next steps. State that sales performance cannot be assessed from these records.",
    },
)


def list_starters():
    return deepcopy(list(STARTERS))


def build_brief(description: str, name: str = "", starter_id: str | None = None) -> dict:
    """Preserve the job description verbatim after trimming outer whitespace."""
    text = description.strip()
    if not 12 <= len(text) <= 8000:
        raise ValueError("Describe the job in 12 to 8,000 characters.")
    starter = next((s for s in STARTERS if s["id"] == starter_id), None)
    if starter_id and starter is None:
        raise ValueError("Unknown starter idea.")
    title = name.strip() or (starter["name"] if starter else "Custom agent")
    if len(title) > 72:
        raise ValueError("Keep the agent name within 72 characters.")
    return {
        "name": title,
        "description": text,
        "instructions": text,
        "starter_id": starter_id,
        "required_data": list(starter["needs"]) if starter else
            ["Choose the documents or records this job should read."],
        "status": "stopped",
        "setup_note": "Test on pasted data, then connect the required records and choose a schedule before live use.",
    }
