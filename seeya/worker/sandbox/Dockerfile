FROM python:3.13-slim

LABEL maintainer="seeya" \
      description="seeya sandbox guest agent"

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Create unprivileged user for the agent
RUN useradd --create-home --shell /bin/bash seeya

WORKDIR /home/seeya

# Copy the guest agent (stdlib-only, no pip install needed)
COPY guest_agent.py /home/seeya/guest_agent.py

RUN chown seeya:seeya /home/seeya/guest_agent.py

USER seeya

EXPOSE 9749

ENTRYPOINT ["python", "-u", "/home/seeya/guest_agent.py"]
CMD ["--host", "0.0.0.0", "--port", "9749"]
