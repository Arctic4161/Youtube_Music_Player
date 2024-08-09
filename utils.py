from os import environ, path


def get_platform():
    kivy_build = environ.get('KIVY_BUILD', '')
    if kivy_build in {'android', 'ios'}:
        return kivy_build
    elif 'P4A_BOOTSTRAP' in environ or 'ANDROID_ARGUMENT' in environ:
        return 'android'
    else:
        return None
