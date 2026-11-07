# The bots' coding sandbox: Hermes's own, plus Rust, since agentshangout is a Rust project.
# setup.sh builds it as aha-sandbox:<SANDBOX_VERSION>; change that number when this file changes.
FROM nousresearch/hermes-sandbox:desktop
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo PATH=/usr/local/cargo/bin:$PATH
# A debug build of agentshangout with debug info and incremental caches takes about 20 GB, and the
# bots each made two or three. One target folder, no debug info and no incremental cache keep a
# build to a few GB; /root is the sandbox's home, which persists, so rebuilds stay quick.
ENV CARGO_TARGET_DIR=/root/target CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0
RUN curl -sSf https://sh.rustup.rs | sh -s -- -y -q --no-modify-path --profile minimal --component clippy,rustfmt \
 && chmod -R a+rwX /usr/local/cargo /usr/local/rustup \
 && ln -s /usr/local/cargo/bin/* /usr/local/bin/ \
 && cargo --version
# Hermes's terminal runs a login shell, which resets PATH: the links above keep cargo on it.
# Hermes forwards GITHUB_TOKEN into the sandbox, but git never reads it on its own and asked the bots
# for a username. This helper hands git the token for github.com, from the environment, never a file.
RUN git config --system credential.https://github.com.helper \
    '!f() { test "$1" = get && echo username=x-access-token && echo "password=$GITHUB_TOKEN"; }; f'
