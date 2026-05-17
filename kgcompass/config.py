from dotenv import load_dotenv
import os
from github import Github

load_dotenv()

# GitHub Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

# LLM credentials
# Prefer the original Bailian variable, but allow local experiments to provide
# a DeepSeek-compatible key directly without rewriting the rest of the code.
BAILIAN_API_KEY = os.getenv("BAILIAN_API_KEY") or os.getenv("DEEPSEEK_API_KEY")

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4jpassword")

# Knowledge Graph Configuration
MAX_CANDIDATE_METHODS = 500
MAX_SEARCH_DEPTH = 2
SEARCH_SPACE = 50

# Connection strength coefficients
CONNECTION_FACTOR = 0.5
WEAK_CONNECTION = 1.0
NORMAL_CONNECTION = WEAK_CONNECTION * CONNECTION_FACTOR
STRONG_CONNECTION = NORMAL_CONNECTION * CONNECTION_FACTOR

# Dataset Configuration
DATASET_NAME = os.getenv("DATASET_NAME", "princeton-nlp/SWE-bench_Lite")
LOCAL_BENCHMARK_PATH = os.getenv("LOCAL_BENCHMARK_PATH")

# Graph Configuration
DECAY_FACTOR = 0.6
VECTOR_SIMILARITY_WEIGHT = 0.3

# API Configuration
# Keep the original DashScope-compatible endpoint as the default, but allow
# local reproduction to override it for direct DeepSeek/OpenAI-compatible APIs.
DEEPSEEK_BASE_URL = os.getenv(
    "OPENAI_COMPAT_BASE_URL",
    os.getenv("DEEPSEEK_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
)
MODEL_NAME = os.getenv("MODEL_NAME", "deepseek-v3")

# LLM Request Configuration
MAX_TOKENS = 4096
TEMPERATURE = 0.3
TOP_P = 0.95

MAX_INPUT_LENGTH = 65536 / 3
LLM_LOC_MAX = 10
CANDIDATE_LOCATIONS_MAX = 20
