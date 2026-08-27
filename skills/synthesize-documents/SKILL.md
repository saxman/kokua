---
name: synthesize-documents
description: Analyze several documents together and synthesize across them. Use when the user asks you to compare, contrast, review, or find themes across multiple papers, reports, or notes, rather than answer a question from one.
license: Apache-2.0
compatibility: Requires the `documents` toolset. Reads from the document store; writes nothing unless asked.
metadata:
  author: kokua
---

# Synthesize across documents

Synthesis is not retrieval. A search returns the passage that answers a question; synthesis needs
every document in view at once, because the output the user wants -- where sources agree, where
they contradict each other, what one measures that another ignores -- lives *between* them. Search
excerpts cannot produce it. This skill exists because the obvious path (search, then summarize the
hits) produces a fluent answer built from first pages, and nothing about that answer looks wrong.

## Steps

1. **List first.** Call `list_documents`. The store is a folder the user can drop files into, so do
   not assume you know what is there. If the listing reports files it could not read, say so now
   and name them: those are almost always PDFs or Word exports, and the user needs to re-export
   them to Markdown before you can use them. Do not silently synthesize from the remainder.

2. **Confirm the set.** Tell the user which documents you are about to work from, by path. If the
   listing is large or ambiguous, ask which ones they mean before reading. Reading the wrong five
   documents costs more than one question.

3. **Read each one in full**, with `read_document`. One call per document. Do not use
   `search_documents` for this step: it returns excerpts, so synthesizing from it means
   synthesizing from the first page of each document.

4. **Extract per document, before comparing.** For each, note in a couple of lines: its central
   claim or research question, its method or evidence, its concrete findings including numbers,
   and the limitations it states about itself. Keep these separate per document. Do not start
   drawing connections yet -- doing the comparison while reading biases every later document
   toward the first one.

5. **Then synthesize across the set.** Organize by *theme*, never document by document. A
   document-by-document walkthrough is a summary, not a synthesis, and the user can already get
   that by reading them. Cover:
   - findings that more than one document supports, and how strong the agreement is
   - direct contradictions, quoting both sides and naming which document said what
   - differences in method that could explain a disagreement before assuming a real one
   - what none of them addresses -- often the most useful part
   Attribute every claim to a document by name. If only one source supports something, say so.

6. **Offer to save it.** If the synthesis is substantial, offer to `save_document` it under
   something like `/synthesis/<topic>.md` so it survives the conversation.

## Notes

- If the documents together are too long to hold at once, do step 4 for each document, then
  discard the full texts and run step 5 over your extracts alone. Degrade this way rather than by
  reading fewer documents: a synthesis over a partial corpus is wrong in a way that is invisible
  to the user.
- If the user asks a narrow factual question ("what did the third paper measure?"), this skill is
  the wrong tool. Use `search_documents` or a single `read_document`.
- Never fill a gap with general knowledge without labelling it. The user asked what *these*
  documents say. If they don't say it, that absence is the finding.
