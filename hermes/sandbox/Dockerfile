# The bots' coding sandbox: Hermes's own, plus Rust, since agentshangout is a Rust project.
# setup.sh builds it as aha-sandbox:<SANDBOX_VERSION>; change that number when this file changes.
FROM nousresearch/hermes-sandbox:desktop
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y -q --no-modify-path --profile minimal --component clippy,rustfmt \
 && chmod -R a+rwX /usr/local/cargo /usr/local/rustup \
 && cargo --version
