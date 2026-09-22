#include <errno.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

/* Keep bundle-owned code alive so TCC attributes Python's capture to the app. */
static volatile sig_atomic_t child_pid = -1;

static void forward_signal(int signal_number) {
    if (child_pid > 0) {
        kill((pid_t)child_pid, signal_number);
    }
}

static char *bootstrap_path(void) {
    uint32_t size = 0;
    if (_NSGetExecutablePath(NULL, &size) != -1 || size == 0) {
        return NULL;
    }

    char *executable = malloc(size);
    if (executable == NULL || _NSGetExecutablePath(executable, &size) != 0) {
        free(executable);
        return NULL;
    }

    char *macos = strrchr(executable, '/');
    if (macos == NULL) {
        free(executable);
        return NULL;
    }
    *macos = '\0';

    const char suffix[] = "/../Resources/launcher.zsh";
    size_t path_size = strlen(executable) + sizeof(suffix);
    char *path = malloc(path_size);
    if (path != NULL) {
        snprintf(path, path_size, "%s%s", executable, suffix);
    }
    free(executable);
    return path;
}

int main(void) {
    char *script = bootstrap_path();
    if (script == NULL) {
        fputs("jev-chat-jarvis: cannot locate launcher.zsh\n", stderr);
        return 1;
    }

    char *const argv[] = {"/bin/zsh", script, NULL};
    pid_t pid = -1;
    int spawn_error = posix_spawn(&pid, "/bin/zsh", NULL, NULL, argv, environ);
    free(script);
    if (spawn_error != 0) {
        fprintf(stderr, "jev-chat-jarvis: cannot start bootstrap: %s\n",
                strerror(spawn_error));
        return 1;
    }
    child_pid = pid;

    signal(SIGINT, forward_signal);
    signal(SIGTERM, forward_signal);
    signal(SIGHUP, forward_signal);

    int status = 0;
    while (waitpid(pid, &status, 0) == -1) {
        if (errno != EINTR) {
            perror("jev-chat-jarvis: waitpid");
            return 1;
        }
    }
    child_pid = -1;

    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 1;
}
