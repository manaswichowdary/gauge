"""Tools the agents can call. All local and deterministic, so tool tasks have fixed answers."""
import ast
import math
import operator

# A small made up company dataset. Models cannot know it without calling the tool.
PRODUCTS = {
    "SKU-1001": {"name": "Trail Runner Shoe", "price": 89.50, "stock": 42, "warehouse": "Phoenix"},
    "SKU-1002": {"name": "Rain Jacket", "price": 129.00, "stock": 7, "warehouse": "Denver"},
    "SKU-1003": {"name": "Water Bottle", "price": 18.25, "stock": 310, "warehouse": "Phoenix"},
    "SKU-1004": {"name": "Headlamp", "price": 34.99, "stock": 0, "warehouse": "Austin"},
    "SKU-1005": {"name": "Camp Stove", "price": 74.00, "stock": 15, "warehouse": "Denver"},
}
EMPLOYEES = {
    "E-201": {"name": "Priya Shah", "team": "Payments", "title": "Software Engineer", "manager": "E-210"},
    "E-210": {"name": "Marcus Lee", "team": "Payments", "title": "Engineering Manager", "manager": "E-310"},
    "E-305": {"name": "Ana Torres", "team": "Search", "title": "Data Engineer", "manager": "E-310"},
    "E-310": {"name": "Ken Watanabe", "team": "Search", "title": "Director", "manager": None},
}
DOCS = [
    {"title": "Refund policy", "text": "Customers can request a refund within 45 days of delivery for unopened items. Opened electronics can be returned within 15 days."},
    {"title": "Shipping", "text": "Standard shipping takes 5 business days. Express shipping takes 2 business days."},
    {"title": "Support hours", "text": "Support is open Monday to Friday, 7am to 6pm Arizona time."},
    {"title": "Warranty", "text": "The Camp Stove has a 3 year warranty. All other products have a 1 year warranty."},
]

# Tool definitions in the Bedrock Converse API format.
TOOL_SPECS = {
    "calculator": {"toolSpec": {
        "name": "calculator",
        "description": "Evaluate an arithmetic expression. Supports + - * / ** % parentheses and sqrt().",
        "inputSchema": {"json": {"type": "object", "properties": {
            "expression": {"type": "string", "description": "For example: (48763 * 9217) or sqrt(144)"}},
            "required": ["expression"]}},
    }},
    "lookup": {"toolSpec": {
        "name": "lookup",
        "description": "Look up a product by SKU (kind=product) or an employee by ID (kind=employee) in the company database.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["product", "employee"]},
            "key": {"type": "string", "description": "A SKU like SKU-1001 or an employee ID like E-201"}},
            "required": ["kind", "key"]}},
    }},
    "fake_search": {"toolSpec": {
        "name": "fake_search",
        "description": "Search the company help center. Returns matching policy documents.",
        "inputSchema": {"json": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}},
    }},
}

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def calculator(expression: str) -> dict:
    """Safe arithmetic: walks the syntax tree instead of using eval, so no code can run."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("exponent too large")
            return _OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "sqrt" and len(node.args) == 1:
            return math.sqrt(ev(node.args[0]))
        raise ValueError("unsupported expression")

    try:
        cleaned = str(expression).replace(",", "").replace("×", "*").replace("^", "**")
        value = ev(ast.parse(cleaned, mode="eval"))
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        return {"result": value}
    except Exception as exc:  # noqa: BLE001 - any bad input becomes a tool error the model can read
        return {"error": f"could not evaluate: {exc}"}


def lookup(kind: str, key: str) -> dict:
    table = {"product": PRODUCTS, "employee": EMPLOYEES}.get(str(kind).lower())
    if table is None:
        return {"error": "kind must be product or employee"}
    record = table.get(str(key).strip().upper())
    return {"key": key.upper(), **record} if record else {"error": f"no {kind} found for {key}"}


def fake_search(query: str) -> dict:
    words = {w.strip(".,?!").lower() for w in str(query).split() if len(w) > 2}
    hits = [d for d in DOCS if words & {w.strip(".,").lower() for w in (d["title"] + " " + d["text"]).split()}]
    return {"results": hits[:3]} if hits else {"results": [], "note": "no matching documents"}


def run_tool(name: str, args: dict) -> dict:
    fn = {"calculator": calculator, "lookup": lookup, "fake_search": fake_search}.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except TypeError as exc:
        return {"error": f"bad arguments: {exc}"}
