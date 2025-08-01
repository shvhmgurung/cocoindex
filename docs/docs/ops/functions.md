---
title: Functions
description: CocoIndex Built-in Functions
---

# CocoIndex Built-in Functions

## ParseJson

`ParseJson` parses a given text to JSON.

The spec takes the following fields:

*   `text` (`str`): The source text to parse.
*   `language` (`str`, optional): The language of the source text.  Only `json` is supported now.  Default to `json`.

Return: *Json*

## SplitRecursively

`SplitRecursively` splits a document into chunks of a given size.
It tries to split at higher-level boundaries. If each chunk is still too large, it tries at the next level of boundaries.
For example, for a Markdown file, it identifies boundaries in this order: level-1 sections, level-2 sections, level-3 sections, paragraphs, sentences, etc.

The spec takes the following fields:

*   `custom_languages` (`list[CustomLanguageSpec]`, optional): This allows you to customize the way to chunking specific languages using regular expressions. Each `CustomLanguageSpec` is a dict with the following fields:
    *   `language_name` (`str`): Name of the language.
    *   `aliases` (`list[str]`, optional): A list of aliases for the language.
        It's an error if any language name or alias is duplicated.

    *   `separators_regex` (`list[str]`): A list of regex patterns to split the text.
        Higher-level boundaries should come first, and lower-level should be listed later. e.g. `[r"\n# ", r"\n## ", r"\n\n", r"\. "]`.
        See [regex syntax](https://docs.rs/regex/latest/regex/#syntax) for supported regular expression syntax.

Input data:

*   `text` (*Str*): The text to split.
*   `chunk_size` (*Int64*): The maximum size of each chunk, in bytes.
*   `min_chunk_size` (*Int64*, optional): The minimum size of each chunk, in bytes. If not provided, default to `chunk_size / 2`.

    :::note

    `SplitRecursively` will do its best to make the output chunks sized between `min_chunk_size` and `chunk_size`.
    However, it's possible that some chunks are smaller than `min_chunk_size` or larger than `chunk_size` in rare cases, e.g. too short input text, or non-splittable large text.

    Please avoid setting `min_chunk_size` to a value too close to `chunk_size`, to leave more rooms for the function to plan the optimal chunking.

    :::

*   `chunk_overlap` (*Int64*, optional): The maximum overlap size between adjacent chunks, in bytes.
*   `language` (*Str*, optional): The language of the document.
    Can be a language name (e.g. `Python`, `Javascript`, `Markdown`) or a file extension (e.g. `.py`, `.js`, `.md`).


    :::note

    We use the `language` field to determine how to split the input text, following these rules:

    *   We match the input `language` field against the following registries in the following order:
        *   `custom_languages` in the spec, against the `language_name` or `aliases` field of each entry.
        *   Builtin languages (see [Supported Languages](#supported-languages) section below), against the language, aliases or file extensions of each entry.

        All matches are in a case-insensitive manner. If the value of `language` is null, it'll be treated as empty string.

    *   If no match is found, the input will be treated as plain text.

    :::

Return: [*KTable*](/docs/core/data_types#ktable), each row represents a chunk, with the following sub fields:

*   `location` (*Range*): The location of the chunk.
*   `text` (*Str*): The text of the chunk.
*   `start` / `end` (*Struct*): Details about the start position (inclusive) and end position (exclusive) of the chunk. They have the following sub fields:
    *   `offset` (*Int64*): The byte offset of the position.
    *   `line` (*Int64*): The line number of the position. Starting from 1.
    *   `column` (*Int64*): The column number of the position. Starting from 1.

### Supported Languages

Currently, `SplitRecursively` supports the following languages:

| Language | Aliases | File Extensions |
|----------|---------|-----------------|
| C | | `.c` |
| C++ | CPP | `.cpp`, `.cc`, `.cxx`, `.h`, `.hpp` |
| C# | CSharp, CS | `.cs` |
| CSS | | `.css`, `.scss` |
| DTD | | `.dtd` |
| Fortran | F, F90, F95, F03 | `.f`, `.f90`, `.f95`, `.f03` |
| Go | Golang | `.go` |
| HTML | | `.html`, `.htm` |
| Java | | `.java` |
| JavaScript | JS | `.js` |
| JSON | | `.json` |
| Kotlin | | `.kt`, `.kts` |
| Markdown | MD | `.md`, `.mdx` |
| Pascal | PAS, DPR, Delphi | `.pas`, `.dpr` |
| PHP | | `.php` |
| Python | | `.py` |
| R | | `.r` |
| Ruby | | `.rb` |
| Rust | RS | `.rs` |
| Scala | | `.scala` |
| SQL | | `.sql` |
| Swift | | `.swift` |
| TOML | | `.toml` |
| TSX | | `.tsx` |
| TypeScript | TS | `.ts` |
| XML | | `.xml` |
| YAML | | `.yaml`, `.yml` |



## SentenceTransformerEmbed

`SentenceTransformerEmbed` embeds a text into a vector space using the [SentenceTransformer](https://huggingface.co/sentence-transformers) library.

:::note Optional Dependency Required

This function requires the 'sentence-transformers' library, which is an optional dependency. Install CocoIndex with:

```bash
pip install 'cocoindex[embeddings]'
```
:::

The spec takes the following fields:

*   `model` (`str`): The name of the SentenceTransformer model to use.
*   `args` (`dict[str, Any]`, optional): Additional arguments to pass to the SentenceTransformer constructor. e.g. `{"trust_remote_code": True}`

Input data:

*   `text` (*Str*): The text to embed.

Return: *Vector[Float32, N]*, where *N* is determined by the model

## ExtractByLlm

`ExtractByLlm` extracts structured information from a text using specified LLM. The spec takes the following fields:

*   `llm_spec` (`cocoindex.LlmSpec`): The specification of the LLM to use. See [LLM Spec](/docs/ai/llm#llm-spec) for more details.
*   `output_type` (`type`): The type of the output. e.g. a dataclass type name. See [Data Types](/docs/core/data_types) for all supported data types. The LLM will output values that match the schema of the type.
*   `instruction` (`str`, optional): Additional instruction for the LLM.

:::tip Clear type definitions

Definitions of the `output_type` is fed into LLM as guidance to generate the output.
To improve the quality of the extracted information, giving clear definitions for your dataclasses is especially important, e.g.

*   Provide readable field names for your dataclasses.
*   Provide reasonable docstrings for your dataclasses.
*   For any optional fields, clearly annotate that they are optional, by `SomeType | None` or `typing.Optional[SomeType]`.

:::

Input data:

*   `text` (*Str*): The text to extract information from.

Return: As specified by the `output_type` field in the spec. The extracted information from the input text.

## EmbedText

`EmbedText` embeds a text into a vector space using various LLM APIs that support text embedding.

The spec takes the following fields:

*   `api_type` ([`cocoindex.LlmApiType`](/docs/ai/llm#llm-api-types)): The type of LLM API to use for embedding.
*   `model` (`str`): The name of the embedding model to use.
*   `address` (`str`, optional): The address of the LLM API. If not specified, uses the default address for the API type.
*   `output_dimension` (`int`, optional): The expected dimension of the output embedding vector. If not specified, use the default dimension of the model.

    For most API types, the function internally keeps a registry for the default output dimension of known model.
    You need to explicitly specify the `output_dimension` if you want to use a new model that is not in the registry yet.

*   `task_type` (`str`, optional): The task type for embedding, used by some embedding models to optimize the embedding for specific use cases.

:::note Supported APIs for Text Embedding

Not all LLM APIs support text embedding. See the [LLM API Types table](/docs/ai/llm#llm-api-types) for which APIs support text embedding functionality.

:::

Input data:

*   `text` (*Str*, required): The text to embed.

Return: *Vector[Float32, N]*, where *N* is the dimension of the embedding vector determined by the model.

## ColPaliEmbedImage

`ColPaliEmbedImage` embeds images using the ColPali multimodal model.

ColPali (Contextual Late-interaction over Patches) uses late interaction between image patch embeddings and text token embeddings for retrieval.

:::note Optional Dependency Required

This function requires the `colpali-engine` library, which is an optional dependency. Install CocoIndex with:

```bash
pip install 'cocoindex[colpali]'
```
:::

The spec takes the following fields:

*   `model` (`str`): The ColPali model name to use (e.g., "vidore/colpali-v1.2")

Input data:

*   `img_bytes` (*Bytes*): The image data in bytes format.

Return: *Vector[Vector[Float32, N]]*, where *N* is the hidden dimension determined by the model. This returns a multi-vector format with variable patches and fixed hidden dimension.

## ColPaliEmbedQuery

`ColPaliEmbedQuery` embeds text queries using the ColPali multimodal model.

This produces query embeddings compatible with ColPali image embeddings for late interaction scoring (MaxSim).

:::note Optional Dependency Required

This function requires the `colpali-engine` library, which is an optional dependency. Install CocoIndex with:

```bash
pip install 'cocoindex[colpali]'
```
:::

The spec takes the following fields:

*   `model` (`str`): The ColPali model name to use (e.g., "vidore/colpali-v1.2")

Input data:

*   `query` (*Str*): The text query to embed.

Return: *Vector[Vector[Float32, N]]*, where *N* is the hidden dimension determined by the model. This returns a multi-vector format with variable tokens and fixed hidden dimension.
