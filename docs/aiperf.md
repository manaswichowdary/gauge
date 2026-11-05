# AIPerf on a local model

[AIPerf](https://pypi.org/project/aiperf/) is NVIDIA's open source tool for load testing model servers. It sends streaming chat
requests and reports time to first token, request latency, inter token latency and throughput. It speaks the OpenAI API, so
it works against a local Ollama server. It does not work against Bedrock, which uses a different API. For Bedrock, use `make load`.

Free. No AWS calls.

## Steps

1. Check your Python version with `python --version`. AIPerf needs 3.11, 3.12 or 3.13. If yours is older, make a second
   virtual environment with a newer Python just for this step.
2. Install it: `pip install -r requirements-aiperf.txt`
3. Install Ollama from ollama.com, then pull the model: `ollama pull qwen2.5:7b`
4. Start the server in another terminal: `ollama serve`
5. Run it: `make aiperf`

AIPerf downloads the model's tokenizer from Hugging Face the first time, so you need internet. The default model
(Qwen 2.5 7B) uses an ungated tokenizer. Llama 3.1 needs you to accept its license on Hugging Face first.

It runs at 1, 2 and 4 concurrent requests, 20 requests each. Change that with
`python -m gauge.cli aiperf --levels 1,2,4,8 --requests 40`.

## Where the results go

- `snapshot/aiperf.json` has the headline numbers per concurrency level. The dashboard shows them.
- `artifacts/aiperf/CON<n>/` has AIPerf's own full output (add `artifacts/` to `.gitignore`, it is large).

## If something fails

Scroll up in the terminal for AIPerf's own error. The usual causes are Ollama not running, the model not pulled, or no internet
for the tokenizer. I have run it end to end on a MacBook Air against Ollama with Qwen 2.5 7B, so the steps above are the ones that worked.
If `snapshot/aiperf.json` has empty `metrics`, send me one of the `profile_export_aiperf.json` files and I will fix it.
