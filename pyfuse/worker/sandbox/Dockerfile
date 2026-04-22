FROM python:3.13-slim

LABEL maintainer="pyfuse" \
      description="pyfuse sandbox guest agent"

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Create unprivileged user for the agent
RUN useradd --create-home --shell /bin/bash pyfuse

WORKDIR /home/pyfuse

# Copy the guest agent (stdlib-only, no pip install needed)
COPY guest_agent.py /home/pyfuse/guest_agent.py

RUN chown pyfuse:pyfuse /home/pyfuse/guest_agent.py

USER pyfuse

EXPOSE 9749

ENTRYPOINT ["python", "-u", "/home/pyfuse/guest_agent.py"]
CMD ["--host", "0.0.0.0", "--port", "9749"]
