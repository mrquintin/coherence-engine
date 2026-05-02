# Legal templates — operator obligation

This directory holds the Jinja2 templates the e-signature service
(prompt 52) renders before sending a document to a provider
(DocuSign / Dropbox Sign).

## NOT LEGAL ADVICE

The files in this directory are **placeholders** that exist only so
the e-signature pipeline is exercisable end-to-end in tests. The
Coherence Engine software does not produce legal advice and does not
warrant the legal sufficiency of any document rendered from these
templates.

**Before sending any signature request in production, the operator
MUST replace each template with a version reviewed and approved by
their securities counsel.** Production templates should also be
versioned (e.g. ``safe_note_v2.docx.j2``) and the prior version kept
on disk so that previously-signed instruments remain reproducible.

## Template variables

Every template receives the variables documented in
``docs/specs/esignature.md``. The pipeline computes a SHA-256 of the
serialized variable map and stores the digest in
``fund_signature_requests.template_vars_hash`` so the rendered
document can be reconstructed exactly without ever persisting the
unsigned body.

## Files

* ``safe_note_v1.docx.j2`` — placeholder Y Combinator post-money
  SAFE. Replace with a counsel-approved DOCX before production use.

## Storage discipline

The unsigned document body is rendered in memory and discarded after
the provider acknowledges the send. Only the signed PDF returned by
the provider is persisted -- it is uploaded to object storage and
the resulting ``coh://`` URI is stored in
``fund_signature_requests.signed_pdf_uri``.
