# PRD Interview Subtask Generation Prompt

Use this prompt with Claude to generate discovery question subtasks from an idea.

---

## Prompt

```
Read the idea bead details below and generate discovery questions.

## Idea Details
**ID**: {{IDEA_ID}}
**Title**: {{IDEA_TITLE}}
**Description**:
{{IDEA_DESCRIPTION}}

---

## Your Role

You are a senior product manager preparing to write a PRD. Before writing, you need to gather more information through discovery questions.

## Process

### Step 1: Analyze the Idea

Review the idea description and identify:
1. **Gaps** - What information is missing?
2. **Ambiguities** - What needs clarification?
3. **Assumptions** - What assumptions need validation?
4. **Risks** - What risks need to be understood?

### Step 2: Generate Discovery Questions

Create 3-7 focused questions that will help write a complete PRD. Questions should cover:

1. **Problem Space** - What problem are we solving? Who has this problem? How painful is it?
2. **Users** - Who are the target users? What are their goals and constraints?
3. **Scope** - What's in scope for v1? What's explicitly out of scope?
4. **Success Criteria** - How will we know this succeeded? What metrics matter?
5. **Constraints** - Technical limitations, timeline, budget, team size?
6. **Existing Solutions** - What exists today? Why is it insufficient?

Not all categories need questions - focus on what's genuinely unclear.

### Step 3: Create Subtasks

For each question, run this command:

```bash
bd create --title="Q: [Short version of question]" \
  --type=task \
  --priority=3 \
  --assignee=human \
  --description="## Question

[Full question text]

## Context

This question relates to the idea: {{IDEA_TITLE}}

## How to Answer

1. Edit this description to add your answer below
2. Or add a comment with your answer
3. Close this task when answered

## Your Answer

[Write your answer here]"
```

After creating each subtask, add a blocking dependency:
```bash
bd dep add {{IDEA_ID}} <subtask-id>
```

This ensures the idea cannot proceed until all questions are answered.

### Step 4: Summary

After creating all subtasks, output:
1. List of questions created
2. The dependency setup
3. Instructions for the human to answer questions

---

## Output Format

When done, output:
```
<interview-complete>
Questions created: [count]
Subtask IDs: [list of IDs]
</interview-complete>
```
```

---

## Usage

This prompt is called by lisa.sh when processing an idea. The script:
1. Reads the idea details with `bd show <id> --json`
2. Substitutes {{IDEA_ID}}, {{IDEA_TITLE}}, {{IDEA_DESCRIPTION}}
3. Runs Claude with this prompt
4. Parses the output to get created subtask IDs
