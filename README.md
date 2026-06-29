# IATI Documentation Machine Translation Prototype

This repo contains a prototype machine translation tool for IATI documentation. 

It uses a combination of deterministic logic and LLM calls to take an IATI docs repo to a state of having full translations to a consistent standard. 

## Testing and current status

This software has been written by @robredpath with extensive use of Claude Code. It has been through ~7 rapid iterations via several dozen prompts. 

This has been tested and refined using the IATI Publisher documentation. It has been run and refined repeatedly, but "works on my machine" caveats may apply. 

It hasn't been tested on any other IATI documentation. It's likely that it will need further refinement as we start to work with a wider range of documentation. 

## Operations

The scripts have three fundamental sets of functionality that are combined in various ways to create useful tooling:
* Using an LLM to translate English text into other languages
* Using an LLM to review translation in the ways that LLMs are better at than deterministic code
* Using deterministic checks to review text in the ways that deterministic code is better at than LLMs

## Usage

Clone the repo; create a venv; install the requirements 
Sign up for a Mistral API key. @robredpath can provide you with one if you'd like. It needs to be a paid account. 

All scripts are run with the format `MISTRAL_API_KEY=XXXXX ./script.py /path/to/an/IATI/docs/repo

## Configuration

glossary.csv should always match the latest version of the IATI Glossary
translation_config.json contains few-shot examples and "translation notes" that are sent with every prompt to LLMs
The glossary is loaded from glossary.csv. Using the format provided by YI, a ui_terms.xlsx translation spreadsheet will be included with the glossary if provided in the target docs repo.

### check_english.py

Reviews the English text to catch spelling, grammar or broken formatting issues before they propagate to the translation

### translate.py

Does whatever is needed to get an IATI docs repo to the state of being translated. 

### stats.py

Gives an overview of the current translation status of the repo 

### review.py

Read-only tool that reviews existing translations and reports issues found by the LLM reviewer. Does not modify files — use translate.py to actually update translations.

## Tests

There is a small unit-test suite covering the deterministic checks (formatting/italics, list prefixes, URL language codes, glossary preservation, length ratios), the PO utilities (obsolete stripping, lock detection), and the LLM JSON parsing. These don't call the API and run in under a second.

```bash
pip install -r requirements-dev.txt
pytest
```


