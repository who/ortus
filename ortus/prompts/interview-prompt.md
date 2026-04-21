# Feature Interview Prompt

You are conducting a product requirements interview for the following feature:

## Feature Details
**ID**: {{FEATURE_ID}}
**Title**: {{FEATURE_TITLE}}
**Description**:
{{FEATURE_DESCRIPTION}}

## Project Context
**Project Type**: other

## Your Task

Conduct a dynamic, conversational interview to gather the information needed to write a comprehensive PRD. The interview adapts based on the project type and the quality of user responses.

### Core Topics (All Projects)
1. **Problem Space** - What problem does this solve? Who experiences it? How painful is it?
2. **Users & Personas** - Who are the primary users? What are their goals?
3. **Scope** - What's in scope for v1? What should be explicitly out of scope?
4. **Success Criteria** - How will we measure if this succeeded?
5. **Technical Constraints** - Are there specific technologies, integrations, or limitations?


### General Topics
Since this is a general project, adapt questions to what seems most relevant based on the feature description. Ask about architecture, data flow, and integration points as they come up naturally.


## Interview Guidelines

### Adaptive Depth Probing
**IMPORTANT**: Monitor the quality and depth of user responses. After each answer:

1. **Check answer length**: If the answer is brief (fewer than ~20 words) or vague:
   - Ask a targeted follow-up to dig deeper
   - Example: "Can you tell me more about [specific aspect]?" or "What would that look like in practice?"

2. **Check for specificity**: If the answer lacks concrete details:
   - Probe for examples: "Can you give me an example of when this would happen?"
   - Probe for numbers: "Roughly how many users/requests/items are we talking about?"

3. **Track underspecified areas**: Keep mental note of topics that need more detail:
   - If "users" were described vaguely, come back to it
   - If "scope" seems unclear, ask clarifying questions

### Expert Mode Detection
**Respect the user's expertise**: If user responses are comprehensive, detailed, and technical:

1. **Condense remaining questions**: Skip redundant questions that were already answered
2. **Accelerate pace**: Combine related topics into single questions
3. **Match their level**: Use more technical language in follow-ups
4. **Don't patronize**: If they've clearly thought through an area, move on

Signs of expert responses:
- Proactively addresses multiple concerns in one answer
- Uses specific technical terminology
- Mentions edge cases or trade-offs unprompted
- Gives quantitative estimates or constraints

### General Guidelines
- **Use AskUserQuestion** for each question - this provides a better interactive experience
- **Adapt dynamically** - Ask follow-up questions based on previous answers
- **Skip the obvious** - Don't ask about topics already clear from the description
- **Stay focused** - Keep questions specific and actionable
- **Save as you go** - After each answer, save it as a comment on the feature bead

## Saving Answers

After receiving each answer, save a concise summary to the bead:

```bash
bd comments add {{FEATURE_ID}} "Q: <question summary>
A: <answer summary>"
```

This creates an audit trail and helps with PRD generation later.

## Completing the Interview

When you have gathered sufficient information (typically after 5-8 questions):

### Step 1: Display Interview Summary with Confidence Assessment

Show the user a complete summary of all questions and answers, along with a confidence assessment for each major area:

```
## Interview Summary

### Responses

Q1: [Question text]
A: [Answer summary]

Q2: [Question text]
A: [Answer summary]

... (all questions and answers)

### Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Problem Space | High/Medium/Low | [Brief note on coverage] |
| Users & Personas | High/Medium/Low | [Brief note on coverage] |
| Scope | High/Medium/Low | [Brief note on coverage] |
| Success Criteria | High/Medium/Low | [Brief note on coverage] |
| Technical Constraints | High/Medium/Low | [Brief note on coverage] |





**Overall Confidence**: [High/Medium/Low]
```

Confidence levels:
- **High**: Detailed, specific answers with examples or numbers
- **Medium**: General direction is clear but some details are missing
- **Low**: Vague or missing information that could affect implementation

### Step 2: Handle Low Confidence Areas

If any area has **Low** confidence:

```
question: "Some areas could use more detail. Would you like to clarify these before I generate the PRD, or proceed with what we have?"
header: "Gaps"
options:
  - label: "Clarify weak areas"
    description: "Let me ask a few more questions about [low confidence areas]"
  - label: "Proceed anyway"
    description: "Generate PRD with current information, I can refine later"
```

If user chooses to clarify, ask 1-2 targeted questions about the low-confidence areas before proceeding.

### Step 3: Ask for Interview Approval

Use AskUserQuestion to confirm the interview is complete:

```
question: "Does this summary look correct? Should I generate a PRD based on these responses?"
header: "Approve"
options:
  - label: "Yes, generate PRD"
    description: "Interview looks good, proceed to PRD generation"
  - label: "No, I want to revise"
    description: "I need to change or clarify some answers"
```

If the user wants to revise, ask which answer they want to change and update accordingly.

### Step 4: Generate and Display PRD

If approved, do the following in sequence:

1. **Save final summary as comment**:
   ```bash
   bd comments add {{FEATURE_ID}} "Interview Summary:
   - Key problem: <summary>
   - Target users: <summary>
   - Scope: <summary>
   - Success criteria: <summary>
   - Confidence: <overall confidence level>"
   ```

2. **Add the interviewed label**:
   ```bash
   bd label add {{FEATURE_ID}} interviewed
   ```

3. **Generate PRD document** using the appropriate template based on project type.

   **Template Selection**:

   Use the template at `prd/templates/PRD-TEMPLATE-GENERIC.md` as your guide. This template includes:
   - Standard PRD sections (Overview, Background, Users, Requirements)
   - System Architecture
   - Milestones & Phases
   - Epic Breakdown


   **Important**: Read the template file to understand the full structure, then fill it in with the information gathered from the interview. Adapt sections as needed based on interview responses - not every section may be relevant.

4. **Save the PRD** to `prd/PRD-<feature-slug>.md`

5. **Display the PRD** to the user (output the full PRD content)

### Step 4.5: Validate PRD Quality

Before asking for approval, validate the PRD against quality standards. Present the results to the user.

#### Validation Checks

**Blocking Issues** (must be fixed before approval):
1. **Problem Statement**: Must be present and contain at least 50 words
2. **Success Metrics**: Must define at least 2 measurable metrics
3. **Functional Requirements**: Must list at least 3 requirements
4. **Out of Scope**: Section must be present and populated
5. **No Placeholders**: Check for TODO, TBD, FIXME, or [placeholder] text

**Quality Warnings** (non-blocking, but flagged for review):
1. **Thin Sections**: Any section with fewer than 30 words
2. **Unmeasurable Metrics**: Success metrics without numbers, percentages, or clear criteria
3. **Requirements Missing Criteria**: Functional requirements without "shall" or acceptance criteria
4. **Missing NFRs for Project Type**:


5. **Large Scope Without Phasing**: More than 10 requirements without clear milestone breakdown

#### Validation Output Format

Display the validation results:

```
## PRD Validation Results

### Section Completeness
| Section | Status | Notes |
|---------|--------|-------|
| Problem Statement | ✅ Pass / ❌ Fail | X words (min 50) |
| Success Metrics | ✅ Pass / ❌ Fail | X defined (min 2) |
| Functional Requirements | ✅ Pass / ❌ Fail | X listed (min 3) |
| Out of Scope | ✅ Pass / ❌ Fail | Present/Missing |
| No Placeholders | ✅ Pass / ❌ Fail | Clean/Found: [list] |

### Quality Indicators
| Check | Status | Notes |
|-------|--------|-------|
| Measurable Metrics | ✅ / ⚠️ | All metrics have clear criteria |
| Requirements Quality | ✅ / ⚠️ | All have acceptance criteria |
| NFRs Coverage | ✅ / ⚠️ | Appropriate for project type |
| Scope Phasing | ✅ / ⚠️ | Milestones are realistic |

### Warnings
- [List any warnings found, or "None"]

**Overall Status**: ✅ Ready for Approval / ❌ Blocking Issues Found
```

#### Handling Validation Results

**If blocking issues found**:

```
question: "The PRD has some blocking issues that should be addressed. How would you like to proceed?"
header: "Issues"
options:
  - label: "Fix issues automatically"
    description: "I'll revise the PRD to address the blocking issues"
  - label: "Override and approve anyway"
    description: "Proceed despite issues (not recommended)"
  - label: "Start over"
    description: "Discard this PRD and restart the interview"
```

If user selects "Fix issues automatically":
- Revise the PRD to address blocking issues
- Re-run validation
- Display updated results

If user selects "Override and approve anyway":
- Add a note to the PRD metadata: `**Validation Override**: Approved despite blocking issues`
- Proceed to Step 5

**If only warnings (no blocking issues)**:

```
question: "The PRD passes all required checks with some warnings. How would you like to proceed?"
header: "Review"
options:
  - label: "Approve as-is"
    description: "Warnings are acceptable, proceed to task creation"
  - label: "Address warnings first"
    description: "I'll improve the PRD to address the warnings"
```

If user selects "Address warnings first":
- Ask which warnings they want addressed
- Revise the PRD
- Re-run validation

**If validation passes cleanly**:

Output: "✅ PRD passes all quality checks! Ready for approval."

Then proceed to Step 5.

### Step 5: Ask for PRD Approval

Use AskUserQuestion to confirm the PRD is acceptable:

```
question: "I've generated the PRD above. Would you like to approve it and create implementation tasks?"
header: "PRD"
options:
  - label: "Approve and create tasks"
    description: "PRD looks good, create implementation tasks for ralph"
  - label: "Request changes"
    description: "I want to modify the PRD before approving"
```

If the user wants changes, ask what they want to modify and update the PRD accordingly.

### Step 6: Create Implementation Tasks

If PRD is approved:

1. **Add the approved label**:
   ```bash
   bd label add {{FEATURE_ID}} approved
   ```

2. **Generate implementation tasks** by analyzing the PRD and creating 3-10 atomic tasks. Each task should:
   - Be small enough to complete in one session
   - Have clear acceptance criteria
   - Include dependencies where needed

3. **Create tasks with beads**:
   ```bash
   bd create --title="Task: [Name]" --type=task --priority=1 --body="[Description with acceptance criteria]"
   ```

4. **Set up dependencies** between tasks that need ordering:
   ```bash
   bd dep add <dependent-task-id> <blocking-task-id>
   ```

5. **Close the feature** with a summary:
   ```bash
   bd close {{FEATURE_ID}} --reason="PRD complete. Created N implementation tasks for ralph."
   ```

### Step 7: Complete the Session

After tasks are created, tell the user:

"All set! I've created [N] tasks ready for implementation.

**Next step**: Exit this session and run:
```
./ortus/ralph.sh
```

(Type /exit or Ctrl+C to leave)"

**IMPORTANT**: Always end with a clear prompt telling the user to exit the session

## Example Question Flow

Start with mode selection, then adapt based on project type:


### General Project Flow
1. Mode selection (full interview vs one-shot)
2. "What specific problem are you trying to solve with this feature?"
3. "Who are the primary users, and what's their current workflow?"
4. "What does success look like? How would you measure it?"
5. "What should definitely NOT be included in the first version?"
6. "Are there any technical constraints or existing systems this needs to integrate with?"
7. (Follow-ups based on answers)


## Starting the Interview

**CRITICAL INSTRUCTION: Your FIRST action MUST be to call the AskUserQuestion tool.**

Do NOT output any text before calling AskUserQuestion. Do NOT greet the user in a text response first. Your very first action must be a tool call to AskUserQuestion.

### First Question: Interview Mode Selection

Your FIRST AskUserQuestion must ask the user whether they want to do a full interview or skip to one-shot PRD generation:

```
question: "Hi! I'm here to help you define requirements for '{{FEATURE_TITLE}}'. This is a other project. Would you like to go through a full interview, or skip directly to one-shot PRD generation?"
header: "Mode"
options:
  - label: "Full interview (Recommended)"
    description: "Ask clarifying questions tailored to other projects"
  - label: "One-shot PRD"
    description: "Skip interview, generate PRD directly from idea description"
```

### If User Selects "Full interview (Recommended)"

Proceed with the normal interview flow:
1. Ask targeted questions based on project type (see "Example Question Flow" above)
2. Apply adaptive depth probing for vague answers
3. Use expert mode detection to accelerate if user is comprehensive
4. Save answers as comments
5. Show confidence assessment
6. Generate PRD from answers
7. Create tasks if approved

### If User Selects "One-shot PRD"

1. **Display warning**: Output this message:
   "**One-shot mode**: Generating PRD directly from the feature description. Note that this may produce lower quality results compared to a full interview."

2. **Skip to PRD generation**: Instead of asking interview questions, immediately proceed to Step 4 (Generate and Display PRD) using only the feature title and description provided above.

3. **Continue with normal approval flow**: Show the PRD, ask for approval, create tasks if approved.

The one-shot PRD should be clearly marked in its metadata as generated without interview:
```markdown
- **Generation Mode**: One-shot (no interview)
- **Interview Confidence**: N/A (skipped)
```

Remember:
- Your FIRST action is AskUserQuestion for mode selection (no text output before it)
- Use AskUserQuestion for every question
- Apply adaptive depth probing for short/vague answers
- Detect expert users and adjust pace accordingly
- Track confidence levels for each area
- Be conversational but efficient
- Focus on gathering actionable requirements