---
title: Expediente
sdk: docker
app_port: 7860
short_description: Bank dispute intake chat in ES and PT, synthetic data
pinned: false
---

# Expediente

Public demo of Expediente, a customer service chat for card and account disputes in Spanish and Portuguese. The customer signs in with a document and a one-time code, picks the movement, confirms the facts, and the bank service opens the case or calls a specialist. Every rule is enforced by the service, and the trace next to the chat shows each step.

Everything here is synthetic: the bank, its customers and their movements come from the hackathon organizers' synthetic dataset. No real person's data is used.

**Try it:** open **Test customers** in the sidebar, pick one, and type the code the simulated phone shows. The **Specialist console** lists the conversations handed to a person. The EN / ES switch changes the presenter and console texts; the chat itself stays in Spanish or Portuguese.

Daily usage is limited and resets at 00:00 UTC. Conversations end when the Space restarts or sleeps.

Code, design and evaluation: <https://github.com/nickolasnolte1/factored-hackathon-2026-nico-x2.exe>

This Space holds code only: the data snapshot, the test customers and the intent model are downloaded at start from a private dataset repo.
