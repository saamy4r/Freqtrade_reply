FROM freqtradeorg/freqtrade:stable
RUN pip install --no-cache-dir freezegun plotly

# The replay code lives in the image, outside /freqtrade/user_data so the
# user_data volume mount never hides it.  Users only mount their user_data.
COPY *.py /opt/freqtrade_replay/freqtrade_replay/

ENTRYPOINT ["python", "/opt/freqtrade_replay/freqtrade_replay/cli.py"]
