FROM python:3.13-slim

LABEL maintainer="away" \
      description="away sandbox guest agent"

# Avoid interactive prompts during package installation
ENV DEBIAN_FRONTEND=noninteractive

# Create unprivileged user for the agent
RUN useradd --create-home --shell /bin/bash away

WORKDIR /home/away

# Copy the guest agent (stdlib-only, no pip install needed)
COPY guest_agent.py /home/away/guest_agent.py

RUN chown away:away /home/away/guest_agent.py

USER away

EXPOSE 9749

ENTRYPOINT ["python", "-u", "/home/away/guest_agent.py"]
CMD ["--host", "0.0.0.0", "--port", "9749"]
