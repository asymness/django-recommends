import logging
from django.contrib.auth.models import User
from django.conf import settings
from ..converters import model_path
from ..similarities import sim_distance
from ..filtering import calculate_similar_items, get_recommended_items
from ..settings import RECOMMENDS_STORAGE_BACKEND, RECOMMENDS_LOGGER_NAME
from ..tasks import remove_suggestions, remove_similarities
from ..utils import import_from_classname


logger = logging.getLogger(RECOMMENDS_LOGGER_NAME)


class RecommendationProviderRegistry(object):
    _vote_providers = {}
    _content_providers = {}

    def __init__(self):
        StorageClass = import_from_classname(RECOMMENDS_STORAGE_BACKEND)
        self.storage = StorageClass(settings)

    def register(self, vote_model, content_models, Provider):
        provider_instance = Provider()
        self._vote_providers[model_path(vote_model)] = provider_instance
        for model in content_models:
            self._content_providers[model_path(model)] = provider_instance

        for signal in provider_instance.rate_signals:
            if isinstance(signal, str):
                sig_class_name = signal.split('.')[-1]
                sig_instance = import_from_classname(signal)
                listener = getattr(provider_instance, sig_class_name, False)
                if listener:
                    for model in content_models:
                        sig_instance.connect(listener, sender=model)

    def get_provider_for_vote(self, model):
        return self._vote_providers[model_path(model)]

    def get_provider_for_content(self, model):
        return self._content_providers[model_path(model)]

    def get_vote_providers(self):
        return self._vote_providers.values()


recommendation_registry = RecommendationProviderRegistry()


class Rating(object):
    def __init__(self, user, rated_object, rating):
        self.user = user
        self.rated_object = rated_object
        self.rating = rating


class RecommendationProvider(object):
    """
    A ``RecommendationProvider`` specifies how to retrieve various informations (items, users, votes)
    necessary for computing recommendation and similarities for a set of objects.

    Subclasses override methods in order to determine what constitutes voted items, a vote,
    its score, and user.
    """
    rate_signals = ['django.db.models.signals.pre_delete']
    similarity = sim_distance

    def __init__(self):
        if not getattr(self, 'storage', False):
            self.storage = recommendation_registry.storage

    def get_items(self):
        """Return items that have been voted"""
        raise NotImplementedError

    def get_ratings(self, obj):
        """Returns all ratings for given item"""
        raise NotImplementedError

    def get_rating_user(self, rating):
        """Returns the user who performed the rating"""
        raise NotImplementedError

    def get_rating_score(self, rating):
        """Returns the score of the rating"""
        raise NotImplementedError

    def get_rating_item(self, rating):
        """Returns the rated object"""
        raise NotImplementedError

    def get_rating_site(self, rating):
        """Returns the site of the rating"""
        return None

    def is_rating_active(self, rating):
        """Returns if the rating is active"""
        return True

    def pre_delete(self, sender, instance, **kwargs):
        """
        This function gets called when a signal in ``self.rate_signals`` is called from the rating model.
        """
        remove_similarities.delay(rated_model=model_path(sender), object_id=instance.id)
        remove_suggestions.delay(rated_model=model_path(sender), object_id=instance.id)

    def vote_list(self):
        vote_list = self.storage.get_votes()
        if vote_list is None:
            vote_list = []
            for item in self.get_items():
                for rating in self.get_ratings(item):
                    user = self.get_rating_user(rating)
                    score = self.get_rating_score(rating)
                    site_id = self.get_rating_site(rating).id
                    identifier = self.storage.get_identifier(item, site_id)
                    vote_list.append((user, identifier, score))
            self.storage.store_votes(vote_list)
        return vote_list

    def precompute(self, vote_list=None):
        """
        This function will be called by the task manager in order
        to compile and store the results.
        """
        if vote_list is None:
            logger.info('fetching votes from the storage...')
            vote_list = self.vote_list()
        logger.info('calculating similarities...')
        itemMatch = self.calculate_similarities(vote_list)

        logger.info('saving similarities...')
        self.storage.store_similarities(itemMatch)
        logger.info('saving suggestions...')
        self.storage.store_recommendations(self.calculate_recommendations(vote_list, itemMatch))

    def get_users(self):
        """Returns all users who have voted something"""
        return User.objects.filter(is_active=True)

    def calculate_similarities(self, vote_list):
        """
        Must return an dict of similarities for every object:

        Accepts a vote matrix representing votes with the following schema:

        ::

            [
                ("<user1>", "<object_identifier1>", <score>),
                ("<user1>", "<object_identifier2>", <score>),
            ]

        Output must be a dictionary with the following schema:

        ::

            [
                ("<object_identifier1>", [
                                (<related_object_identifier2>, <score>),
                                (<related_object_identifier3>, <score>),
                ]),
                ("<object_identifier2>", [
                                (<related_object_identifier2>, <score>),
                                (<related_object_identifier3>, <score>),
                ]),
            ]

        """
        return calculate_similar_items(vote_list, similarity=self.similarity)

    def calculate_recommendations(self, vote_list, itemMatch):
        """
        ``itemMatch`` is supposed to be the result of ``calculate_similarities()``

        Returns a list of recommendations:

        ::

            [
                (<user1>, [
                    ("<object_identifier1>", <score>),
                    ("<object_identifier2>", <score>),
                ]),
                (<user2>, [
                    ("<object_identifier1>", <score>),
                    ("<object_identifier2>", <score>),
                ]),
            ]

        """
        recommendations = []
        for user in self.get_users():
            rankings = get_recommended_items(vote_list, itemMatch, user)
            recommendations.append((user, rankings))
        return recommendations