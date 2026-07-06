# AreaPulse Architecture

## Principles

* Clean Architecture
* SOLID
* Dependency Inversion
* Repository Pattern
* Service Layer
* Separation of Concerns
* DTO-based communication
* Stateless APIs
* Testability first
* Scalability first

## Architecture Layers

Presentation

↓

DTO

↓

Application Service

↓

Domain Service

↓

Repository Interface

↓

Infrastructure

↓

Database / AI / Redis / Storage / External Services

## Rules

* Controllers must not contain business logic.
* Controllers must not directly access the database.
* AI modules must not import database code.
* Business logic belongs in services.
* Database logic belongs in repositories.
* External APIs should be wrapped behind services.
* Validation should happen through DTOs.
* Code should be easy to test independently.
* Prefer composition over inheritance.
* Prefer loose coupling over tight coupling.

