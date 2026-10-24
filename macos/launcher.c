/*
 * Lanciatore di PolarLab.app: incorpora l'interprete Python ed esegue `python -m polarlab`
 * dalla cartella del progetto, con il suo ambiente virtuale.
 *
 * Perché non uno script shell: il Python di Homebrew è una build "framework" che si
 * rilancia come Python.app, quindi il Dock mostrerebbe "Python" e macOS chiederebbe il
 * permesso Bluetooth per un Info.plist senza NSBluetoothAlwaysUsageDescription.
 * Incorporando libpython il processo resta PolarLab.app: nome, icona e permesso Bluetooth
 * sono i suoi. Il codice Python si legge sempre dalla cartella del progetto, quindi l'app
 * non va ricostruita quando il codice cambia.
 *
 * La cartella del progetto è la chiave `PolarLabProjectDir` dell'Info.plist (scritta da
 * scripts/build_mac_app.sh). L'output va in ~/Library/Logs/PolarLab/polarlab.log.
 */

#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <CoreFoundation/CoreFoundation.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static void alert(const char *message)
{
    CFStringRef msg = CFStringCreateWithCString(NULL, message, kCFStringEncodingUTF8);
    CFOptionFlags response;
    CFUserNotificationDisplayAlert(0, kCFUserNotificationStopAlertLevel, NULL, NULL, NULL,
                                   CFSTR("PolarLab non si avvia"), msg, CFSTR("OK"), NULL, NULL, &response);
    CFRelease(msg);
    fprintf(stderr, "PolarLab: %s\n", message);
}

static int info_path(const char *key, char *out, size_t size)
{
    CFStringRef k = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
    CFTypeRef v = CFBundleGetValueForInfoDictionaryKey(CFBundleGetMainBundle(), k);
    CFRelease(k);
    if (v == NULL || CFGetTypeID(v) != CFStringGetTypeID())
        return 0;
    return CFStringGetFileSystemRepresentation((CFStringRef)v, out, (CFIndex)size);
}

static void open_log(void)
{
    const char *home = getenv("HOME");
    char dir[PATH_MAX], path[PATH_MAX];
    if (home == NULL)
        return;
    snprintf(dir, sizeof dir, "%s/Library/Logs/PolarLab", home);
    mkdir(dir, 0755);
    snprintf(path, sizeof path, "%s/polarlab.log", dir);
    if (freopen(path, "a", stdout) != NULL && freopen(path, "a", stderr) != NULL) {
        setvbuf(stdout, NULL, _IOLBF, 0);
        setvbuf(stderr, NULL, _IONBF, 0);
    }
}

int main(int argc, char **argv)
{
    char project[PATH_MAX], python[PATH_MAX], message[PATH_MAX + 256];

    open_log();
    if (!info_path("PolarLabProjectDir", project, sizeof project)) {
        alert("Nell'Info.plist manca PolarLabProjectDir. Ricostruisci l'app con scripts/build_mac_app.sh.");
        return 1;
    }
    if (chdir(project) != 0) {
        snprintf(message, sizeof message,
                 "Cartella del progetto non trovata:\n%s\n\nSe l'hai spostata, ricostruisci l'app con "
                 "scripts/build_mac_app.sh dalla nuova posizione.", project);
        alert(message);
        return 1;
    }
    snprintf(python, sizeof python, "%s/venv.nosync/bin/python3", project);
    if (access(python, X_OK) != 0) {
        snprintf(message, sizeof message,
                 "Ambiente virtuale non trovato:\n%s\n\nCrealo una volta con:\npython3.13 -m venv venv.nosync && "
                 "venv.nosync/bin/pip install -r requirements.txt", python);
        alert(message);
        return 1;
    }

    /* Il pacchetto polarlab sta nella cartella del progetto (con -m da Terminale la aggiunge la cwd) */
    setenv("PYTHONPATH", project, 1);

    PyStatus status;
    PyConfig config;
    PyConfig_InitPythonConfig(&config);
    config.buffered_stdio = 0;
    config.parse_argv = 0;
    /* L'interprete trova venv.nosync/pyvenv.cfg accanto a `executable` e ne usa le site-packages */
    status = PyConfig_SetBytesString(&config, &config.executable, python);
    if (PyStatus_Exception(status))
        goto fail;
    status = PyConfig_SetBytesString(&config, &config.program_name, python);
    if (PyStatus_Exception(status))
        goto fail;
    status = PyConfig_SetString(&config, &config.run_module, L"polarlab");
    if (PyStatus_Exception(status))
        goto fail;

    /* sys.argv: file aperti con l'app (zip dall'iPhone), senza l'argomento -psn_ del Finder */
    char **args = calloc((size_t)argc + 1, sizeof(char *));
    int n = 0;
    args[n++] = python;
    for (int i = 1; i < argc; i++)
        if (strncmp(argv[i], "-psn_", 5) != 0)
            args[n++] = argv[i];
    status = PyConfig_SetBytesArgv(&config, n, args);
    free(args);
    if (PyStatus_Exception(status))
        goto fail;

    status = Py_InitializeFromConfig(&config);
    if (PyStatus_Exception(status))
        goto fail;
    PyConfig_Clear(&config);
    return Py_RunMain();

fail:
    PyConfig_Clear(&config);
    if (PyStatus_IsExit(status))
        return status.exitcode;
    snprintf(message, sizeof message, "Python non si è avviato: %s",
             status.err_msg ? status.err_msg : "errore sconosciuto");
    alert(message);
    return 1;
}
