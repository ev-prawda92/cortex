# Illustrative payloads

All records are synthetic and unsigned. The permit and receipt examples show decoded payload shapes, not credentials. The certificate thumbprint, policy digest, and context digest are placeholders without corresponding trusted records. Timestamps are fixed example times and must not be used as current validity claims.

`action.canonical.json` is the exact UTF-8 canonical byte sequence, without a trailing newline, for `action.json`. The linked action digest is recomputed by the validator. This ASCII/integer-only example does not test the full Unicode or number handling requirements of JCS.

The REVIEW decision and unreviewed permit illustrate separate possible outcomes; they are not a valid approval-to-execution trace. The first retained UNKNOWN receipt illustrates recovery when no earlier receipt was persisted. A production implementation must obtain authority records and real signatures.
