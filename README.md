# RAG Research Assistant

A full-stack application for querying scientific papers using Retrieval-Augmented Generation (RAG) with vector search.

## Features

- 📄 **Paper Management** - Add arXiv papers to your personal library via URL
- 💬 **Chat Interface** - Ask questions about your papers with streaming responses
- 🔍 **Vector Search** - Semantic search using ChromaDB and sentence-transformers
- 📊 **LaTeX Support** - Mathematical formulas render as compiled equations
- 🔗 **Interactive Sources** - Click citations to open PDFs at specific pages
- 🎨 **Dark Theme** - Modern dark interface with Tailwind CSS
- 🔐 **User Authentication** - JWT-based authentication with secure password hashing
- ⚙️ **Model Selection** - Choose any OpenRouter model and use custom API keys

## Stack

| Layer | Technology |
|-------|------------|
| Frontend | Next.js 15, TypeScript, Tailwind CSS, shadcn/ui |
| Backend | FastAPI (Python 3.11) |
| Vector DB | ChromaDB with sentence-transformers |
| LLM | OpenRouter API (any model) |
| Auth | JWT + argon2 password hashing |
| arXiv | arxiv library + PyMuPDF for PDF parsing |
| Container | Docker + docker-compose |

## How it works

Here's the big picture — what talks to what:

```mermaid
flowchart LR
    User(["👤 You"])

    subgraph App["🖥️ Web App · Next.js"]
        UI["Chat &amp; Library"]
    end

    subgraph Server["⚙️ Backend · FastAPI"]
        Auth["🔐 Auth"]
        Papers["📄 Papers"]
        Chat["💬 Chat / RAG"]
    end

    subgraph Data["💾 Your data (local)"]
        DB[("SQLite<br/>accounts · papers · chats")]
        Vec[("ChromaDB<br/>paper embeddings")]
        Files[("PDF files")]
    end

    subgraph Web["🌍 Internet"]
        Arxiv["arXiv.org"]
        LLM["OpenRouter LLM"]
    end

    User <--> UI
    UI <--> Auth
    UI <--> Papers
    UI <--> Chat

    Auth --> DB
    Papers --> DB
    Papers --> Files
    Papers --> Arxiv
    Papers --> Vec

    Chat --> DB
    Chat --> Vec
    Chat <--> LLM

    classDef user fill:#fef3c7,stroke:#f59e0b,color:#000
    classDef app fill:#dbeafe,stroke:#3b82f6,color:#000
    classDef srv fill:#e0e7ff,stroke:#6366f1,color:#000
    classDef data fill:#dcfce7,stroke:#16a34a,color:#000
    classDef ext fill:#fce7f3,stroke:#ec4899,color:#000

    class User user
    class UI app
    class Auth,Papers,Chat srv
    class DB,Vec,Files data
    class Arxiv,LLM ext
```

### 📥 Adding a paper

1. You paste an arXiv URL in the sidebar.
2. The backend downloads the PDF and pulls the paper's metadata.
3. The text is split into small chunks and turned into embeddings — locally, with `sentence-transformers`.
4. Embeddings land in ChromaDB; the paper shows up in your library.

### 💬 Asking a question

1. You type a question in chat.
2. The backend embeds your question and finds the most relevant chunks across your papers (ChromaDB top-k search).
3. Those chunks plus your question are sent to the LLM via OpenRouter.
4. The answer streams back token-by-token, with clickable citations that jump to the exact PDF page.

> 🔐 Your account is gated by a JWT issued at login (password hashed with argon2). Everything except registration and login requires that token.

## Quick Start

### Prerequisites
- Docker and Docker Compose
- OpenRouter API key (get free at https://openrouter.ai)

### Setup

```bash
# 1. Clone and navigate
git clone <repo-url>
cd ragsystem

# 2. Configure environment
cp .env.example .env
# Edit .env and add your OpenRouter API key

# 3. Run with Docker
docker-compose build
docker-compose up
```

Access at `http://localhost:3000`

### Local Development

```bash
# Backend
cd backend
pip install -r requirements.txt
uvicorn main:app --reload

# Frontend (separate terminal)
cd frontend
npm install
npm run dev
```

## Usage

1. **Register** - Create account
2. **Add Papers** - Paste arXiv URLs in right sidebar
3. **Chat** - Ask questions about your papers
4. **Settings** - Choose model and add custom API key

## API Endpoints

- `POST /api/auth/register` - Register
- `POST /api/auth/login` - Login
- `GET /api/papers` - List papers
- `POST /api/papers` - Add paper
- `DELETE /api/papers/{id}` - Remove paper
- `POST /api/chat/sessions` - Create chat
- `POST /api/chat/sessions/{id}/messages` - Send message (streams)

## License

MIT
