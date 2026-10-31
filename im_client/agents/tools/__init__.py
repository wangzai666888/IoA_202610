import os

# Capture tool containers intentionally avoid importing unrelated browser, RAG,
# academic-search and device packages. The original ReAct tool set is preserved
# for containers without SIM_URL.
if os.environ.get("SIM_URL"):
    from simulator.ioa_tools import TOOL_FUNCTIONS

    TOOL_MAPPING = dict(TOOL_FUNCTIONS)
    BROWSER_TOOLS_MAPPING = {}
else:
    from . import scholarly_tools
    from . import arxiv_tools
    from . import rag_tools
    from . import playwright_browser
    from . import code_executor
    from . import youtube
    from . import wikidata

    TOOL_MAPPING = {
        "arxiv_search": arxiv_tools.arxiv_search,
        "scholarly_search_author": scholarly_tools.scholarly_search_author,
        "scholarly_search_keyword": scholarly_tools.scholarly_search_keyword,
        "scholarly_search_pubs": scholarly_tools.scholarly_search_pubs,
        "execute_code": code_executor.execute_code,
        "search_youtube": youtube.search_youtube,
        "get_youtube_transcript": youtube.get_youtube_transcript,
        "search_wikidata": wikidata.search_wikidata,
    }

    BROWSER_TOOLS_MAPPING = {
        "informational_web_search": playwright_browser.informational_web_search,
        "navigational_web_search": playwright_browser.navigational_web_search,
        "visit_page": playwright_browser.visit_page,
        "download_file": playwright_browser.download_file,
        "page_up": playwright_browser.page_up,
        "page_down": playwright_browser.page_down,
        "read_page_and_answer": playwright_browser.read_page_and_answer,
        "summarize_page": playwright_browser.summarize_page,
    }
