import os

def load_agent_prompt(rule_name: str, skills: list = None) -> str:
    """Loads a persona rule and appends specified skills to create a comprehensive prompt."""
    base_path = ".agents"
    prompt = ""
    
    # Load Rule
    rule_path = os.path.join(base_path, "rules", f"{rule_name}.md")
    if os.path.exists(rule_path):
        with open(rule_path, 'r') as f:
            prompt += f.read()
    else:
        prompt += f"# Agent: {rule_name.upper()}\n(Rule file missing: {rule_path})\n"
    
    prompt += "\n\n## Specialized Skills\n"
    
    # Load Skills
    if skills:
        for skill in skills:
            skill_path = os.path.join(base_path, "skills", f"{skill}.md")
            if os.path.exists(skill_path):
                with open(skill_path, 'r') as f:
                    prompt += f"\n--- Skill: {skill} ---\n"
                    prompt += f.read()
    
    return prompt

# Dynamically generated prompts
# 1. PRD Agent (using pm persona + governance logic)
PRD_AGENT_PROMPT = load_agent_prompt("pm", ["governance_strategy", "multi_agent_handoff"])

# 2. Design Agent (uses developer persona + architect focus)
DESIGN_AGENT_PROMPT = load_agent_prompt("developer", ["governance_strategy"])

# 3. Dev Agent (Logic)
DEV_LOGIC_AGENT_PROMPT = load_agent_prompt("developer", ["data_steward_ops", "governance_strategy"])

# 4. Dev Agent (UI)
DEV_UI_AGENT_PROMPT = load_agent_prompt("developer", ["multi_agent_handoff"])

# 5. Test Agent
TEST_AGENT_PROMPT = load_agent_prompt("tester", ["data_steward_ops"])

# 6. Validation Agent
VALIDATION_AGENT_PROMPT = load_agent_prompt("security", ["governance_strategy", "multi_agent_handoff"])

if __name__ == "__main__":
    print("Testing prompt loading...")
    print(f"PRD Agent Prompt Length: {len(PRD_AGENT_PROMPT)} characters.")
    print(f"Test Agent Prompt Length: {len(TEST_AGENT_PROMPT)} characters.")
