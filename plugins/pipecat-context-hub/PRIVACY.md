# Pipecat Context Hub plugin privacy notice

Last updated: 7 October 2026

## Local Context Hub processing

Context Hub runs locally in the environment where you install it. Its source
copies, documentation, code excerpts, metadata, embeddings and search index are
stored there. Query processing, embedding inference and reranking run on that
same host using local models. Context Hub does not send your search queries or
index to a hosted Context Hub or Daily search service. Chroma product telemetry
is disabled.

When you use the plugin in a managed cloud task, that task's execution
environment is the host. Its storage and access rules apply; it is separate
from your own computer.

## Information shared with your chat application

The chat application receives the prompts you send it and the selected query
arguments and results used by the assistant. Retrieved documentation, example
code and API excerpts can therefore appear in ChatGPT's context, including
excerpts from private sources you choose to index and retrieve. Chat data and
execution-host logs are subject to the policies and controls of that application
and host. Local search processing does not mean that chat content stays on your
computer.

The plugin supplies workflow instructions and assets. It does not operate a
publisher-hosted query database or automatically upload the local index.

## Installation and indexing downloads

Authorised installation and index refresh contact package registries, source
repositories, documentation sites and model hosts to download the required
software, sources and models. Those services receive the requests needed for
the downloads. These operations are separate from searching the existing local
index. A populated index is reused; refresh and repair require separate
authorisation.

## Pipecat Cloud deployment

Deployment requires a Pipecat Cloud account and login. Approved Cloud operations
send account and deployment requests and the selected application build inputs
to Pipecat Cloud. The plugin's deployment workflow presents the target and
upload contents before execution. Context Hub's index is not automatically part
of the application upload.

Daily's handling of Pipecat Cloud account and service data is covered by
[Daily's privacy policy](https://www.daily.co/legal/privacy/), including its
data-use, sharing, retention and privacy-rights information. See the
[Daily Trust Center](https://trust.daily.co/) for further security and privacy
information. Cloud use is also subject to
[Daily's terms of service](https://www.daily.co/legal/terms-of-service/).

Complete login through the CLI's browser flow or secure credential mechanism.
Do not put passwords, login tokens or API key values into chat. No credentials
are included in this plugin package.

## Retention and user controls

The local index and downloaded sources and models remain in the execution
environment until you remove them or that environment's storage is cleared.
Context Hub does not apply an automatic retention period to these local files.
You control which sources are indexed and which queries are run. Removing the
Hub data directory and model caches removes those local copies; this does not
delete information already included in chat or uploaded to a Cloud service.

Use your chat application's controls for chat history and your Pipecat Cloud
account controls for deployed resources. Daily's privacy policy describes the
rights and contact routes for data held by Daily. The execution host's policies
govern any additional host logs, caches or backups.

For Context Hub plugin support, use the
[project's GitHub issues page](https://github.com/pipecat-ai/pipecat-context-hub/issues).
Information you post there is handled by GitHub and may be public.
