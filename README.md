# Lenz — AI-Powered Intelligent Video Surveillance

<p align="center">
  <strong>Turning Every Camera into an Intelligent Guardian</strong>
</p>

<p align="center">
  Transforming ordinary CCTV cameras into intelligent,
  AI-powered monitoring systems for safer and smarter environments.
</p>

<p align="center">
  🚨 Threat Detection • 👥 Crowd Monitoring • 🚗 Parking Intelligence • 📊 Real-Time Insights
</p>

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Problem Understanding & Approach](#2-problem-understanding--approach)
3. [Proposed Solution](#3-proposed-solution)
4. [Key Features](#4-key-features)
5. [System Architecture](#5-system-architecture)
6. [System Workflow](#6-system-workflow)
7. [AI & Computer Vision Approach](#7-ai--computer-vision-approach)
8. [Technology Stack](#8-technology-stack)
9. [Database Design](#9-database-design)
10. [Dataset & Model Selection](#10-dataset--model-selection)
11. [API Documentation & Testing](#11-api-documentation--testing)
12. [Installation & Setup](#12-installation--setup)
13. [End-to-End Workflow](#13-end-to-end-workflow)
14. [Demo & Screenshots](#14-demo--screenshots)
15. [Module-Wise Development](#15-module-wise-development)
16. [Team Roles & Responsibilities](#16-team-roles--responsibilities)
17. [Future Scope](#17-future-scope)
18. [Known Limitations](#18-known-limitations)
19. [Privacy & Responsible AI](#19-privacy--responsible-ai)
20. [Impact](#20-impact)

---

## 1. Problem Statement

### Problem Title

**AI-Powered Intelligent Video Surveillance & Real-Time Threat Detection**

### Problem Description

Millions of CCTV cameras are installed across restaurants, railway
stations, classrooms, hospitals, shopping malls, parking areas,
offices, and public streets.

These cameras continuously record video footage, but most surveillance
systems still depend heavily on human operators to monitor camera feeds
and identify important events.

With multiple cameras recording simultaneously, it becomes difficult
for security personnel to observe every feed continuously.
As a result, theft, suspicious activities, parking violations,
and overcrowded areas may go unnoticed until someone manually
reviews the footage or reports an incident.

Traditional CCTV systems primarily act as recording devices rather
than intelligent monitoring systems.

The problem is not the lack of cameras, but the lack of intelligence
in the information they capture.

Lenz aims to bridge this gap by using Artificial Intelligence (AI)
and Computer Vision to analyse video feeds, identify predefined
events, and generate actionable alerts for security personnel.

The goal is to transform existing camera infrastructure into
an intelligent monitoring system that helps people identify
potential problems faster and make informed decisions.

---

### Key Challenges

| Challenge | Description | Impact |
|---|---|---|
| **Continuous Manual Monitoring** | Security personnel must observe multiple camera feeds simultaneously. | Important incidents may be missed. |
| **Delayed Theft Detection** | Suspicious activities may remain unnoticed until footage is reviewed. | Delayed response and potential losses. |
| **Crowd Management** | Operators may struggle to estimate crowd density across multiple locations. | Overcrowding and delayed intervention. |
| **Parking Violations** | Unauthorized parking and occupied spaces may not be monitored efficiently. | Congestion and inefficient parking management. |
| **Information Overload** | Cameras generate large volumes of footage throughout the day. | Reviewing recordings consumes time and effort. |
| **Delayed Incident Response** | Conventional systems may require someone to notice an incident before action is taken. | Slower security response. |
| **Limited Actionable Insights** | Raw video footage does not automatically provide structured event summaries. | Difficult monitoring and decision-making. |

---

### Current Situation vs Desired Outcome

| Current Situation | Desired Outcome |
|---|---|
| Cameras continuously record video. | AI analyses video frames to identify relevant events. |
| Operators manually monitor camera feeds. | Automated event detection assists human operators. |
| Incidents may be noticed after they occur. | Timely alerts help operators investigate potential incidents. |
| Crowd levels are estimated manually. | Computer vision estimates crowd density in supported camera views. |
| Parking violations require manual observation. | AI-assisted detection identifies predefined parking violations. |
| Large volumes of footage require manual review. | Event-based summaries help operators locate relevant footage. |
| Security teams react after receiving reports. | Intelligent monitoring supports faster, more informed responses. |

---
## 3. Target Users

The Lenz system is designed for organizations that already use CCTV
cameras and want to improve surveillance through AI-powered monitoring.

```mermaid
flowchart TD
flowchart TD
    A["LENZ AI Surveillance System"]

    A --> B["Railway Stations"]
    A --> C["Shopping Malls"]
    A --> D["Hospitals"]
    A --> E["Educational Institutions"]
    A --> F["Parking Facilities"]
    A --> G["Restaurants and Offices"]

    B --> H["Crowd Monitoring"]
    C --> I["Crowd and Security Monitoring"]
    D --> J["Occupancy Monitoring"]
    E --> K["Restricted Area Monitoring"]
    F --> L["Vehicle and Parking Detection"]
    G --> M["Suspicious Activity Alerts"]

    style A fill:#EDE9FE,stroke:#8B5CF6,color:#222
    style B fill:#EDE9FE,stroke:#8B5CF6,color:#222
    style C fill:#EDE9FE,stroke:#8B5CF6,color:#222
    style D fill:#EDE9FE,stroke:#8B5CF6,color:#222
    style E fill:#EDE9FE,stroke:#8B5CF6,color:#222
    style F fill:#EDE9FE,stroke:#8B5CF6,color:#222
    style G fill:#EDE9FE,stroke:#8B5CF6,color:#222
---
### Target Market Analysis

| Category | Details |
|---|---|
| **Geographical Focus** | India 🇮🇳 — with potential for global adoption |
| **Primary Market** | Organizations using existing CCTV infrastructure |
| **Target Customers** | Railway stations, malls, hospitals, educational institutions, offices, and parking facilities |
| **Existing Infrastructure** | CCTV cameras and compatible video feeds |
| **Core Opportunity** | Transforming passive video surveillance into AI-assisted monitoring |
| **Primary Objective** | Improve situational awareness and help identify important events faster |
| **Initial Deployment Strategy** | Pilot deployment in a controlled environment with a limited number of cameras |
| **Expansion Strategy** | Extend to additional camera feeds and monitoring locations after validation |

---

### User Segmentation

| User Segment | Description | Key Needs |
|---|---|---|
| **Security Personnel** | Personnel responsible for monitoring camera feeds and responding to incidents. | Event alerts, suspicious activity review, and faster incident identification. |
| **Railway Authorities** | Teams responsible for station safety and passenger movement. | Crowd density estimation and overcrowding alerts. |
| **Mall Administrators** | Managers overseeing public areas and parking facilities. | Crowd monitoring, vehicle detection, and security alerts. |
| **Hospital Administrators** | Teams managing patient waiting areas and public corridors. | Occupancy insights and crowd monitoring. |
| **Educational Institutions** | Administrators responsible for designated campus areas. | Restricted-area monitoring and security event alerts. |
| **Parking Operators** | Personnel responsible for parking space utilization. | Vehicle detection, occupancy monitoring, and parking violation alerts. |
| **Business Owners** | Organizations managing restaurants, offices, and commercial facilities. | Centralized monitoring and actionable event summaries. |
| **System Administrators** | Personnel responsible for operating and maintaining Lenz. | Camera configuration, system health, access management, and event logs. |

---
### Operational Requirements

| Parameter | Description |
|---|---|
| **Video Input** | Compatible CCTV streams, supported cameras, or uploaded video files. |
| **Monitoring Frequency** | Configurable frame processing depending on the model and hardware. |
| **Detection Categories** | Supported objects, crowd conditions, and predefined events. |
| **Alert Generation** | Alerts based on configured detection rules and thresholds. |
| **Event Records** | Event category, timestamp, camera identifier, and available detection metadata. |
| **Human Verification** | Operator review of alerts before consequential action. |
| **Scalability** | Ability to add supported camera feeds as system capacity permits. |
| **Privacy Requirements** | Controlled access to footage, limited retention, and appropriate authorization. |

---
## 4. Existing Gap vs Lenz Improvement

### Traditional CCTV vs Lenz

Traditional CCTV systems primarily record footage, whereas Lenz aims
to analyse supported video feeds and surface relevant events for review.

The following conceptual comparison illustrates the intended
capabilities of the proposed system.

![Traditional CCTV vs Lenz Capability Comparison](assets/lenz_capability_comparison.png)

**Note:** The scores in this graph are illustrative values used to
visualize the proposed concept. They are not measured benchmark
results, verified model accuracy, or evidence of actual system
performance. They should be replaced with results from controlled
testing when available.

---



