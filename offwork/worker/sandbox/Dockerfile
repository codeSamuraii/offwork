FROM python:3.13-slim

LABEL maintainer="offwork" \
      description="offwork sandbox guest agent"

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Create unprivileged user for the agent
RUN useradd --create-home --shell /bin/bash offwork

WORKDIR /home/offwork

# Copy the guest agent (stdlib-only, no pip install needed)
COPY guest_agent.py /home/offwork/guest_agent.py

RUN chown offwork:offwork /home/offwork/guest_agent.py

USER offwork

EXPOSE 9749

ENTRYPOINT ["python", "-u", "/home/offwork/guest_agent.py"]
CMD ["--host", "0.0.0.0", "--port", "9749"]
