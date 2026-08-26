# CORTEX Agents: Integration Requirements

This document specifies what integrations each agent needs to move from demo/mock to production, the technical complexity, and recommended approaches.

---

## 1. No-Show Outreach Agent

### Purpose
Proactively identifies high-risk patients and sends personalized outreach to reduce no-shows.

### Current Mock Data
- Patient list with no-show risk scores
- Contact preferences (phone, email, SMS)
- Appointment schedule

### Required Integrations

#### 1.1 Patient Data & No-Show Risk Prediction
**System**: Electronic Health Record (EHR) + Analytics
**Data Required**:
- Patient demographics
- Appointment history (scheduled vs. completed)
- Historical no-show patterns
- Social determinants (transportation barriers, language, etc.)

**Technical Approach**:
```
Option A: Direct EHR API (most common)
- Epic FHIR API (/Patient, /Appointment endpoints)
- Cerner SMART on FHIR
- athenaHealth API

Option B: Data warehouse query
- Snowflake/BigQuery table: patients_no_show_risk
- Pre-computed risk scores updated daily
- Example query:
  SELECT patient_id, no_show_risk_score
  FROM risk_model_predictions
  WHERE risk_score > 75 AND appointment_date = TOMORROW()
```

**Complexity**: Medium
- Risk prediction requires historical data pipeline
- Pre-compute risk scores daily (better than real-time)
- FHIR endpoints are standardized but auth setup varies by EHR

#### 1.2 Contact Information
**System**: EHR + Patient communication preferences
**Data Required**:
- Phone number (validated, opted-in for SMS)
- Email address
- Preferred language
- Preferred communication channel
- Opt-out history

**Technical Approach**:
```
REST call to EHR:
GET /api/v1/patients/{patient_id}/contact-info
GET /api/v1/patients/{patient_id}/communication-preferences

Response schema:
{
  "phone": "+1-555-0101",
  "email": "john.smith@email.com",
  "preferred_channel": "sms",
  "preferred_language": "en",
  "sms_opted_in": true,
  "last_updated": "2026-08-01"
}
```

**Complexity**: Low
- Available in most EHRs via standard API
- Usually cached/safe to query frequently

#### 1.3 Message Delivery (SMS, Email, Phone)
**System**: Healthcare communications platform
**Options**:
- **SMS**: Twilio (healthcare-compliant), Telnyx
- **Email**: HIPAA-compliant email (Office 365, Google Workspace with BAA)
- **Phone**: Voicecare, Bandwidth, CallRail (healthcare versions)

**Technical Approach**:
```python
# SMS via Twilio (HIPAA-compliant account required)
from twilio.rest import Client
client = Client(TWILIO_ACCOUNT, TWILIO_TOKEN)
message = client.messages.create(
    to=patient_phone,
    from_=CORTEX_TWILIO_NUMBER,
    body="Hi John, reminder of your appointment tomorrow at 10am with Dr. Williams..."
)

# Email via healthcare email gateway
import smtplib
# Uses SMTP with HIPAA provider (HIPAA BAA required)
server = smtplib.SMTP(HEALTHCARE_SMTP_HOST, 587)
server.send_message(msg)  # Message object includes PHI
```

**Complexity**: Low-Medium
- Third-party services handle compliance
- Requires BAA agreements
- Rate limiting & retry logic needed

#### 1.4 Audit & Compliance Logging
**System**: Audit log (separate from production)
**Required Data**:
- Who (agent ID)
- What (patient ID, message sent)
- When (timestamp)
- How (channel, status)
- Outcome (delivered, failed, bounced)

**Technical Approach**:
```
Log entry schema:
{
  "event_type": "outreach_message_sent",
  "agent_id": "no-show-outreach-01",
  "patient_id": "P001",
  "timestamp": "2026-08-26T14:30:00Z",
  "channel": "sms",
  "status": "delivered",
  "message_id": "MSG-001",
  "outcome": "delivered"
}

Storage: Immutable append-only log
- Database table with DATE partition
- CloudWatch/Datadog ingestion
- Queryable for compliance audits
```

**Complexity**: Low
- Standard audit logging pattern
- Usually built into healthcare platforms

### Integration Effort Estimate
- **Time**: 1-2 weeks
- **Dependencies**: EHR API access, SMS provider account, audit system setup
- **Risk**: Medium (PHI handling, contact validation)

---

## 2. Appointment Reminder Agent

### Purpose
Sends appointment reminders 24-48 hours before scheduled visits.

### Required Integrations

#### 2.1 Appointment Schedule
**System**: EHR scheduling system
**Data Required**:
- Appointment date/time
- Patient ID, name
- Provider name, specialty
- Location, room
- Appointment type (telehealth vs. in-person)

**Technical Approach**:
```
Scheduled query (run every 4 hours):
GET /api/v1/appointments?status=scheduled&date_range=next_48_hours
Response includes patient contact info already joined

Alternative: Event-driven
- Listen to EHR appointment creation webhook
- Queue reminders with scheduled send time
```

**Complexity**: Low-Medium
- Most EHRs have appointment endpoints
- Webhooks reduce polling overhead

#### 2.2 Patient Contact & Preferences
**System**: Same as No-Show Outreach (1.2)

#### 2.3 Message Delivery
**System**: SMS/Email (same as 1.3)

#### 2.4 Two-Way Messaging (Optional but valuable)
**Feature**: Patients can reply "yes" / "no" / "reschedule"
**System**: Conversational SMS/Email platform
**Options**: 
- Twilio Conversations API
- Bandwidth Application Platform
- Healthcare-specific: PatientConnect, SimplePractice

**Technical Approach**:
```
Setup inbound webhook:
POST /cortex/appointment-reminder-reply
{
  "patient_id": "P001",
  "appointment_id": "APT001",
  "reply": "yes|no|reschedule_requested",
  "timestamp": "2026-08-27T10:15:00Z"
}

Trigger workflow:
- "yes" → log confirmation, reduce no-show risk
- "no" / "reschedule" → notify provider/scheduler
```

**Complexity**: Medium
- Adds conversational component
- Requires NLP/intent parsing or simple keyword matching

### Integration Effort Estimate
- **Time**: 1 week (basic) to 2 weeks (with two-way messaging)
- **Dependencies**: EHR scheduling API, communications platform
- **Risk**: Low (mostly read-only, notifications are low-risk)

---

## 3. Lab Result Notification Agent

### Purpose
Notifies patients of lab results with appropriate clinical context; escalates critical values.

### Required Integrations

#### 3.1 Lab Results Data
**System**: EHR + Laboratory Information System (LIS)
**Data Required**:
- Result status (pending, final, corrected)
- Test name and code
- Result values
- Reference ranges
- Abnormal flags
- Test comments/interpretations

**Technical Approach**:
```
Real-time trigger (best):
- Lab LIS sends HL7 OBX (observation result) message
- Or webhook: POST /cortex/lab-result-available
  {
    "patient_id": "P001",
    "result_id": "LAB001",
    "test_code": "CBC",
    "status": "final",
    "result_date": "2026-08-26",
    "values": [
      {"code": "WBC", "value": 7.2, "unit": "K/uL", "normal_range": "4.5-11.0", "abnormal": false}
    ]
  }

Polling fallback:
GET /api/v1/patients/{patient_id}/lab-results?status=final&since=2026-08-25
```

**Complexity**: Medium-High
- Lab integration is often the slowest (legacy HL7, not modern APIs)
- Reference ranges vary by patient demographics
- Result status tracking (pending→final→corrected) needs careful handling

#### 3.2 Clinical Context & Provider Notes
**System**: EHR
**Data Required**:
- Ordering provider
- Clinical indication
- Recent visit notes (diagnosis, treatment plan)
- Previous results for comparison

**Technical Approach**:
```
GET /api/v1/patients/{patient_id}/encounters?limit=5
GET /api/v1/patients/{patient_id}/problems
→ Use to contextualize results in notification
```

**Complexity**: Low
- Standard EHR API calls

#### 3.3 Critical Value Detection & Escalation
**System**: Rules engine + provider notification
**Data Required**:
- Critical value thresholds (standardized + institution-specific)
- On-call provider roster
- Escalation routing

**Technical Approach**:
```
Decision tree:
1. Compare result value against critical thresholds
   - Use CLIA/CAP standards + custom institutional thresholds
2. If critical:
   - Immediately notify provider (phone > email)
   - Page on-call provider if primary unavailable
   - Log incident for compliance
3. If abnormal:
   - Notify patient with guidance
   - Schedule provider follow-up
4. If normal:
   - Simple notification

Critical thresholds example:
{
  "WBC": {"critical_low": "<2.0", "critical_high": ">30"},
  "Potassium": {"critical_low": "<2.5", "critical_high": ">6.0"},
  "Glucose": {"critical_low": "<40", "critical_high": ">600"}
}
```

**Complexity**: High
- Requires knowledge of clinical standards
- Escalation logic is high-stakes
- Needs testing & provider sign-off

#### 3.4 Patient Notification with Clinical Guidance
**System**: Secure messaging platform + SMS/Email
**Challenge**: How to communicate results clearly without alarming patients
**Approach**:
```
For NORMAL results:
"Your lab results are back and look good. All values are normal. 
Schedule a follow-up if you have questions → Contact Dr. Williams"

For ABNORMAL results:
"Your recent cholesterol test shows elevated levels (240 mg/dL, normal <200). 
This is not an emergency, but your doctor wants to discuss diet and 
possibly treatment options. Dr. Williams will reach out within 2 days → Schedule appointment"

For CRITICAL results:
"We detected an urgent lab result that needs immediate attention. 
Dr. Williams is being notified and will call you shortly. 
If this is a life-threatening emergency, call 911."
```

**Complexity**: Medium
- Requires clinical input on messaging
- HIPAA-secure patient portal messaging is best
- Plain SMS should be generic ("results available, check your portal")

### Integration Effort Estimate
- **Time**: 2-3 weeks
- **Dependencies**: Lab LIS integration (slowest), EHR API, clinical escalation rules, secure messaging platform
- **Risk**: High (clinical decision point, critical values escalation)

---

## 4. Prior Authorization Agent

### Purpose
Manages prior authorization requests with insurance payers (most integration-heavy).

### Required Integrations

#### 4.1 Prior Auth Request Generation
**System**: EHR ordering system
**Data Required**:
- Service/procedure ordered
- Provider (NPI, credentials)
- Patient (member ID, DOB)
- Clinical diagnosis & justification
- Estimated cost
- Provider office contact info

**Technical Approach**:
```
Event-driven from EHR:
When provider places certain orders → trigger auth request
POST /cortex/prior-auth-request
{
  "patient_id": "P001",
  "service": "MRI Brain",
  "cpt_code": "70553",
  "diagnosis_icd10": "R51.9",
  "provider_npi": "1234567890",
  "office_phone": "555-0100",
  "clinical_justification": "New onset headaches, r/o structural lesion"
}

Identify which payers need auth:
- Query benefits table: Which services require prior auth for this patient?
- Hit EHR benefit check API: Does this service need auth?
```

**Complexity**: Medium
- Requires integration with EHR order entry
- Payer benefit determination is complex (coverage rules vary)

#### 4.2 Payer Connectivity (THE BIG ONE)
**System**: Insurance payer systems
**This is the largest integration task.**

**Option A: HL7 EDI 837 (X12) Electronic Submission**
```
Standard: ASC X12-EDI 837 (healthcare claim/auth format)
- Most payers still use this (legacy but universal)
- Requires healthcare EDI transmission service:
  - Emdeon, Relay, Availity, Zenadoc (clearinghouses)
  - Handle formatting, validation, transmission
- Setup:
  1. Register with clearinghouse
  2. Negotiate agreements with each payer
  3. Get transmission credentials
  4. Implement X12 837 generation
  
Example Python library:
  from x12.loop import Loop
  # Build 837 message manually (no standard lib exists)
  
Typical turnaround: 1-2 business days per request
```

**Option B: Direct Payer APIs**
```
Modern approach (10-15% of payers offer this):
- Blue Cross Blue Shield (some markets): API available
- Aetna, UnitedHealth: Proprietary APIs (limited)
- Cigna: EDI-primarily but exploring APIs

Example: UnitedHealth Authorization Portal API
POST /api/v1/prior-authorizations
{
  "request": {
    "member_id": "ABC123456",
    "service": "MRI Brain",
    "clinical_indication": "Headache evaluation"
  }
}
Response:
{
  "auth_id": "AUTH-123456",
  "status": "pending",
  "decision_by": "2026-08-28"
}

Problem: Each payer's API is different
  → Need adapter pattern for each payer
```

**Option C: Healthcare-Specific SaaS (Recommended Path)**
```
Services like:
- Change Healthcare (largest EDI clearinghouse)
- Allscripts (integrates with EHR)
- Waystar (prior auth platform)
- GNW (payer connectivity platform)

They provide:
1. Pre-built payer connections
2. Format translation (your data → each payer's format)
3. Submission & tracking
4. REST API to send requests

Example with Change Healthcare API:
POST /api/prior-auth/v1/requests
{
  "memberId": "ABC123456",
  "providerId": "1234567890",
  "serviceType": "MRI",
  "icd10": "R51.9"
}
Response:
{
  "requestId": "CH-AUTH-001",
  "status": "submitted",
  "payers": ["Blue Shield", "Aetna"],
  "estimatedResponseTime": "1-3 business days"
}

Cost: $500-5000/month depending on volume
```

**Recommended Approach**:
1. **Short term (MVP)**: Use healthcare EDI platform (Change Healthcare, Waystar)
   - Covers 80-90% of US payers
   - Outsource payer-specific formatting
   - Focus on business logic, not EDI parsing
   
2. **Long term**: Add direct payer APIs as needed
   - Only major payers in your market
   - Custom adapters per payer

**Complexity**: VERY HIGH
- Payers are fragmented
- Legacy systems still dominate
- Requires dedicated effort

#### 4.3 Status Checking & Follow-Up
**System**: Payer response feeds + automated checking
**Approach**:
```
Inbound from payer (async):
- HL7 271 (Eligibility/Auth response) from clearinghouse
- Parse: Approved / Denied / More Info Needed
- Trigger workflow based on status

Polling (if payer doesn't provide real-time):
GET /api/v1/prior-auth/{auth_id}/status (every 24 hours)

Auto-follow-up:
If "More Info Needed" → Automatically extract additional docs from EHR
If "Denied" → Alert provider with reason code & appeal process
If no response after X days → Re-submit
```

**Complexity**: Medium
- Status tracking state machine
- Integration with notification system

#### 4.4 Provider & Patient Notification
**System**: Secure messaging, email, phone
**Workflow**:
```
Approved:
→ Auto-schedule procedure
→ Notify patient: "Your authorization is approved, you're all set"
→ Notify provider: "AUTH approved, patient ID [X], valid through [DATE]"

Denied:
→ Notify provider with denial reason (coverage limitation, diagnosis not covered, etc.)
→ Provide appeal instructions
→ Alert patient: "Your requested service needs further discussion"
→ May require manual intervention

Pending > 5 days:
→ Auto-alert provider & patient
→ Offer to call payer for expedite
```

**Complexity**: Low-Medium
- Mostly notification orchestration

### Integration Effort Estimate
- **Time**: 4-8 weeks
- **Dependencies**: 
  - **Critical path**: Payer connectivity (EDI or SaaS platform) = 3-4 weeks
  - EHR order integration = 1 week
  - Clinical documentation retrieval = 1 week
  - Notification system = 1 week
- **Risk**: Very High (payment processing impact, payer relationships, state regulations)
- **Cost**: $5,000-50,000+ (clearinghouse fees, platform licenses)

---

## Integration Complexity Pyramid

```
                ▲
               /│\
              / │ \  PRIOR AUTH (Very High)
             /  │  \ (Payer connectivity, 4-8 weeks)
            /───┼───\
           /    │    \
          /     │     \ LAB RESULT (High)
         /      │      \ (Clinical escalation, 2-3 weeks)
        /───────┼───────\
       /        │        \
      /         │         \ APPOINTMENT REMINDER (Medium)
     /          │          \ (Scheduling + messaging, 1-2 weeks)
    /───────────┼───────────\
   /            │            \
  / NO-SHOW OUTREACH (Medium)  \
 /  (Analytics + messaging,    \
/    1-2 weeks)                \
```

---

## Shared Infrastructure

These agents all need:

### 1. **Message Queue** (for async operations)
- For scheduling sends at specific times
- Technologies: RabbitMQ, AWS SQS, Kafka
- Example: "Send reminder in 24 hours" → message queued with deferred send

### 2. **Secure Data Store** (for PHI)
- HIPAA-compliant database
- Encryption at rest (AWS KMS, Azure Key Vault)
- Audit logging on every access
- Technologies: AWS RDS with encryption, Azure SQL Database

### 3. **Authentication & Authorization**
- OAuth2 for EHR APIs
- Service accounts for inter-system calls
- Audit trail of all credentials used

### 4. **Monitoring & Alerting**
- CloudWatch/Datadog for agent health
- PagerDuty for critical failures
- SLA tracking per agent

### 5. **HIPAA Compliance**
- Business Associate Agreements (BAAs) with all vendors
- Incident response procedures
- Annual risk assessments
- Staff training

---

## Recommended Implementation Sequence

1. **Start with Appointment Reminder** (lowest risk, fastest ROI)
   - EHR scheduling is mature
   - SMS platforms are simple
   - Fast to market (1-2 weeks)

2. **Then No-Show Outreach** (builds on Reminder infrastructure)
   - Reuses messaging platform
   - Adds analytics/prediction layer
   - Good for patient engagement story

3. **Then Lab Result Notification** (highest clinical value)
   - More complex escalation logic
   - Requires clinical input
   - Worth the effort for patient experience

4. **Finally Prior Authorization** (highest business impact but longest)
   - Plan this in parallel but execute last
   - Requires payer negotiations
   - 4-8 week effort justified by revenue protection

---

## Checklist for Each Integration

- [ ] API documentation reviewed
- [ ] Authentication method chosen (OAuth2, API key, basic auth)
- [ ] Rate limiting understood (QPS limits, throttling strategy)
- [ ] Error handling implemented (retry logic, fallback behavior)
- [ ] Data validation in place (required fields, formats, ranges)
- [ ] Audit logging configured (who, what, when, where)
- [ ] HIPAA compliance verified (encryption, BAA signed)
- [ ] Test environment available
- [ ] Production credentials secured (vault, secret manager)
- [ ] Monitoring and alerting configured
- [ ] Runbook written for common failures
- [ ] Disaster recovery plan (what if service goes down?)
